# Connect to bol

1. Create API credentials in the bol seller dashboard (*Instellingen → Diensten → API instellingen*).
   In this dev environment use **demo** credentials only.
2. Open **Bol Settings** (System Manager or Bol Manager). Fill in *Client ID*, *Client Secret*,
   *Company*. Choose *Environment* (*Demo* by default).
3. Save, then click **Test Connection**. It fetches a token and lists open orders. An error message
   says whether the credentials were rejected or bol couldn't be reached.
4. Tick **Enabled** to switch the integration on. Saving never calls bol.

## Logs

Every failed bol call and every successful write (POST/PUT/PATCH/DELETE) is recorded in
**Ecommerce Integration Log** (integration *Bol*), with request, response, traceback and the related
document (*Reference DocType / Name*). Tick **Debug Logging** to log successful GETs too. Turn it
off again: logs hold buyer data. Authorization headers, tokens and the client secret are masked.
Only System Manager and Bol Manager can read the logs.

## Tokens

Tokens are cached in Redis only, until 30 s before they expire. Saving Bol Settings clears the
cached token.
