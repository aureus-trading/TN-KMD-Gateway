import os
import sqlite3 as sqlite
import requests
import time
import base58
import PyCWaves
import traceback
import sharedfunc
import bitcoinrpc.authproxy as authproxy
from verification import verifier

class TNChecker(object):
    def __init__(self, config):
        self.config = config
        self.dbCon = sqlite.connect('gateway.db')

        self.node = self.config['tn']['node']
        self.verifier = verifier(config)

        cursor = self.dbCon.cursor()
        self.lastScannedBlock = cursor.execute('SELECT height FROM heights WHERE chain = "TN"').fetchall()[0][0]

    def getCurrentBlock(self):
        #return current block on the chain - try/except in case of timeouts
        try:
            CurrentBlock = requests.get(self.node + '/blocks/height').json()['height'] - 1
        except:
            CurrentBlock = 0

        return CurrentBlock

    def run(self):
        #main routine to run continuesly
        print('started checking tn blocks at: ' + str(self.lastScannedBlock))

        self.dbCon = sqlite.connect('gateway.db')
        while True:
            try:
                nextblock = self.getCurrentBlock() - self.config['tn']['confirmations']

                if nextblock > self.lastScannedBlock:
                    self.lastScannedBlock += 1
                    self.checkBlock(self.lastScannedBlock)
                    cursor = self.dbCon.cursor()
                    cursor.execute('UPDATE heights SET "height" = ' + str(self.lastScannedBlock) + ' WHERE "chain" = "TN"')
                    self.dbCon.commit()
            except Exception as e:
                self.lastScannedBlock -= 1
                print('Something went wrong during tn block iteration: ')
                print(traceback.TracebackException.from_exception(e))

            time.sleep(self.config['tn']['timeInBetweenChecks'])

    def checkBlock(self, heightToCheck):
        #check content of the block for valid transactions
        block =  requests.get(self.node + '/blocks/at/' + str(heightToCheck)).json()
        for transaction in block['transactions']:
            if self.checkTx(transaction):
                targetAddress = base58.b58decode(transaction['attachment']).decode()
                otherProxy = authproxy.AuthServiceProxy(self.config['other']['node'])
                valAddress = otherProxy.validateaddress(targetAddress)
                zvalAddress = otherProxy.z_validateaddress(targetAddress)

                if not(valAddress['isvalid']) and not(zvalAddress['isvalid']):
                    self.faultHandler(transaction, "txerror")
                else:
                    amount = round(transaction['amount'] / pow(10, self.config['tn']['decimals']))
                    amount -= self.config['other']['fee']

                    if amount <= 0:
                        self.faultHandler(transaction, "senderror", e='under minimum amount')
                    else:
                        try:
                            passphrase = os.getenv(self.config['other']['passenvname'], self.config['other']['passphrase'])

                            if len(passphrase) > 0:
                                otherProxy.walletpassphrase(passphrase, 30)

                            if valAddress['isvalid']:
                                txId = otherProxy.sendtoaddress(targetAddress, amount)
                            elif zvalAddress['isvalid']:
                                fromaddress = self.config['other']['gatewayAddress']
                                todata = {'address': targetAddress, 'amount': amount}
                                txdata = [todata]
                                opId = otherProxy.z_sendmany(fromaddress, txdata)
                                opId = [opId]
                                time.sleep(5)
                                txId = otherProxy.z_getoperationresult(opId)

                                while len(txId) == 0:
                                    time.sleep(5)
                                    txId = otherProxy.z_getoperationresult(opId)

                            if 'error' in txId[0]:
                                self.faultHandler(transaction, "senderror", e=txId[0])
                            else:
                                txId = txId[0]['result']['txid']

                            if len(passphrase) > 0:
                                otherProxy.walletlock()

                            if 'error' in txId:
                                self.faultHandler(transaction, "senderror", e=txId)
                            else:
                                print("send tx: " + txId)

                                cursor = self.dbCon.cursor()
                                #amount /= pow(10, self.config['other']['decimals'])
                                cursor.execute('INSERT INTO executed ("sourceAddress", "targetAddress", "tnTxId", "otherTxId", "amount", "amountFee") VALUES ("' + transaction['sender'] + '", "' + targetAddress + '", "' + transaction['id'] + '", "' + txId + '", "' + str(round(amount)) + '", "' + str(self.config['other']['fee']) + '")')
                                self.dbCon.commit()
                                print(self.config['main']['name'] + ' tokens withdrawn from tn!')
                        except Exception as e:
                            self.faultHandler(transaction, "txerror", e=e)

                        if txId.startswith('opid'):
                            self.verifier.verifyOther(txId, z=True)
                        else:
                            self.verifier.verifyOther(txId)

    def checkTx(self, tx):
        #check the transaction
        if tx['type'] == 4 and tx['recipient'] == self.config['tn']['gatewayAddress'] and tx['assetId'] == self.config['tn']['assetId']:
            #check if there is an attachment
            targetAddress = base58.b58decode(tx['attachment']).decode()
            if len(targetAddress) > 1:
                #check if we already processed this tx
                cursor = self.dbCon.cursor()
                result = cursor.execute('SELECT otherTxId FROM executed WHERE tnTxId = "' + tx['id'] + '"').fetchall()

                if len(result) == 0: return True
            else:
                self.faultHandler(tx, 'noattachment')

        return False
        
    def faultHandler(self, tx, error, e=""):
        #handle transfers to the gateway that have problems
        amount = tx['amount'] / pow(10, self.config['tn']['decimals'])
        timestampStr = sharedfunc.getnow()

        if error == "noattachment":
            cursor = self.dbCon.cursor()
            cursor.execute('INSERT INTO errors ("sourceAddress", "targetAddress", "otherTxId", "tnTxId", "amount", "error") VALUES ("' + tx['sender'] + '", "", "", "' + tx['id'] + '", "' + str(amount) + '", "no attachment found on transaction")')
            self.dbCon.commit()
            print(timestampStr + " - Error: no attachment found on transaction from " + tx['sender'] + " - check errors table.")

        if error == "txerror":
            targetAddress = base58.b58decode(tx['attachment']).decode()
            cursor = self.dbCon.cursor()
            cursor.execute('INSERT INTO errors ("sourceAddress", "targetAddress", "otherTxId", "tnTxId", "amount", "error", "exception") VALUES ("' + tx['sender'] + '", "' + targetAddress + '", "", "' + tx['id'] + '", "' + str(amount) + '", "tx error, possible incorrect address", "' + str(e) + '")')
            self.dbCon.commit()
            print(timestampStr + " - Error: on outgoing transaction for transaction from " + tx['sender'] + " - check errors table.")

        if error == "senderror":
            targetAddress = base58.b58decode(tx['attachment']).decode()
            cursor = self.dbCon.cursor()
            cursor.execute('INSERT INTO errors ("sourceAddress", "targetAddress", "otherTxId", "tnTxId", "amount", "error", "exception") VALUES ("' + tx['sender'] + '", "' + targetAddress + '", "", "' + tx['id'] + '", "' + str(amount) + '", "tx error, check exception error", "' + str(e) + '")')
            self.dbCon.commit()
            print(timestampStr + " - Error: on outgoing transaction for transaction from " + tx['sender'] + " - check errors table.")
