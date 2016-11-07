"""Interface to Sage accounting ODBC

This provides an interface to extract data from the accounting system.

It works by extracting the data into a Pandas dataframe and then doing queries from that.

"""
import json
import numpy as np
import pandas as pd
import pyodbc
import os

from luca import p


class PySageError(Exception):
    pass

def get_default_connection_string():
    try:
        try:
            # Python 2
            connection_string = os.environ['PYSAGE_CNXN'].decode('utf8')
        except AttributeError:
            # Python 3
            connection_string = os.environ['PYSAGE_CNXN']
    except KeyError:
        raise PySageError('Environment missing PYSAGE_CNXN setting')
    return connection_string


def get_max_transaction_in_sage(cnxn):
    sql = """
SELECT
    max(TRAN_NUMBER)
FROM
    AUDIT_JOURNAL
    """
    df = pd.read_sql(sql, cnxn)
    return int(df.iloc[0,0])

def get_dataframe_sage_odbc_query(sql, name):
    """This executes a SQL query if it needs to or pulls in a json file from disk.
    The results of the SQL query are returned as a dataframe.  To decide which to do
    the maximum transaction is compared to the json file."""
    connection_string = get_default_connection_string()
    cnxn = pyodbc.connect(connection_string)
    # Get the maximum transaction number
    json_check_file_name = name + '_check.json'
    json_file_name = name + '.json'
    # Read it from file
    try:
        with open(json_check_file_name) as f:
            data = json.load(f)
        max_transaction_stored = data['max_transaction_stored']
    except (FileNotFoundError, ValueError):  # Triggered as open nonexistent file is ok but no data
        max_transaction_stored = 0
    max_transaction_in_sage = get_max_transaction_in_sage(cnxn)
    if max_transaction_stored == 0 or max_transaction_stored != max_transaction_in_sage:
        df = pd.read_sql(sage_all_data, cnxn)
        # Read fresh data from sage
        # Update files
        df.to_json(json_file_name)
        data = {'max_transaction_stored': max_transaction_in_sage}
        with open(json_check_file_name, 'w') as f:
            json.dump(data, f)
    else:  # read memoised data
        df = pd.read_json(json_file_name)
        # Need to fix those records that are integer but normally stored as strings.  On memoization theses are
        # converted to integers so now need to be converted back to strings to be compatible
        for fn in ['ACCOUNT_REF', 'INV_REF']:
            df[fn] = df[fn].astype('str')
    return df


sage_all_data = """
SELECT
    aj.TRAN_NUMBER, aj.TYPE, aj.DATE, nl.ACCOUNT_REF, aj.ACCOUNT_REF as ALT_REF, aj.INV_REF, aj.DETAILS, AJ.TAX_CODE,
    aj.AMOUNT, aj.FOREIGN_AMOUNT, aj.BANK_FLAG, ah.DATE_BANK_RECONCILED, aj.EXTRA_REF
FROM
NOMINAL_LEDGER nl, AUDIT_HEADER ah
LEFT OUTER JOIN AUDIT_JOURNAL aj ON nl.ACCOUNT_REF = aj.NOMINAL_CODE
WHERE
aj.HEADER_NUMBER = ah.HEADER_NUMBER AND
aj.DATE > '2000-01-01' AND aj.DELETED_FLAG = 0
"""


class Sage:
    """Interface to SAGE line 50 account system.
    """
    def  __init__(self, connection_string=''):
        if connection_string == '':
            connection_string = get_default_connection_string()
        self.sqldata = get_dataframe_sage_odbc_query(sage_all_data, 'SageODBC')
        if self.sqldata['DATE'].dtype == np.object:
            self.sqldata['DATE'] = self.sqldata['DATE'].astype('datetime64')

    def using_invoice_get(self, i, field, numchars=30):
        """
                Using the invoice number we can look up the field.  The accounting database contains line entries.
                So this aggregates the line entries and returns the sum of the field if numeric.
        """
        df = self.sqldata[(self.sqldata['TYPE'] == 'SI')
                          & (self.sqldata['ACCOUNT_REF'] == '1100')
                          & (self.sqldata['INV_REF'].str.contains(str(i)))
                          ]
        if len(df) == 0:  # It is an error to look up data where their is none
            raise PySageError('No data found in Audit Header to match invoice {}'.format(i))
        elif field == 'TRAN_NUMBER':
            return list(df[:1][field])[0]
        elif field in ['DATE', 'TYPE', 'ACCOUNT_REF', 'ALT_REF', 'INV_REF', 'TAX_CODE',
                       'BANK_FLAG', 'DATE_BANK_RECONCILED']:
            return list(df[field])[0]
        elif field in ['AMOUNT', 'FOREIGN_AMOUNT', 'NET_AMOUNT']:
            return p(df[field].sum())
        elif field in ['DETAILS', 'EXTRA_REF']:
            return df[field].str.cat()[:numchars]
        else:
            raise PySageError('Unmatched get field {} for using_invoice_get '.format(field))

    def get_field(self, row, field):
        """ For use in a lambda
         lambda row: self.get_field(row,'This Field')
        """
        result = None
        if row['Member Code'] not in ('4552', '4424'):  # TODO Ignore enrichment for AIS discount and AIS invoices
            if row['Document Type'] in ('Invoice', 'Credit Note',):
                result = self.using_invoice_get(row['Your Ref'], field)
        return result

    def enrich_remittance_doc(self, remittance_doc):
        """Enrich a raw remittance document with data from Sage
        """
        def get_series(field):
            return remittance_doc.df.apply(lambda row: self.get_field(row, field), axis=1)

        remittance_doc.df['Account_Ref'] = get_series('ACCOUNT_REF')
        remittance_doc.df['Sage_Net_Amount'] = get_series('NET_AMOUNT')
        remittance_doc.df['Sage_Gross_Amount'] = get_series('GROSS_AMOUNT')
        remittance_doc.df['Sage_VAT_Amount'] = get_series('TAX_AMOUNT')
        remittance_doc.df['Sage_Tax_Rate'] = get_series('TAX_RATE') / 100
        net = remittance_doc.df['Sage_Net_Amount'].sum()
        vat = remittance_doc.df['Sage_VAT_Amount'].sum()
        gross = remittance_doc.df['Sage_Gross_Amount'].sum()
        # Check sage calculations - shouldn't be a problem.  if this is passed can then rely on two of the
        # three values to set the third.  Note due to rounding you can't calculate them except approximately unless
        # you have access to the line items.
        if ( p(net + vat) != p(gross) ):
            remittance_doc.checked = False
            raise PySageError("Internal calcs of sum in Sage don't add up. net + vat != gross,  {} + {} != {}".format(
                net, vat, gross
            ))
        # Check that gross AIS doc values match Sage gross values  TODO remove specific for local installation
        gross_sum_ex_discount = remittance_doc.df[remittance_doc.df['Member Code'] != '4552']['Sage_Gross_Amount'].sum()
        if (gross != gross_sum_ex_discount):
            remittance_doc.checked = False
            raise PySageError("Adding up total AIS invoices doesn't equal Sage sum,  {} != {}, types {}, {}".format(
                gross_sum_ex_discount, gross, type(gross_sum_ex_discount), type(gross)
            ))
        # The internal sum has already been done.  It is not until the next stage that we calculate discounts