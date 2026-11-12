# bol.com integration for ERPNext

**Marketplace Integrations** is an open-source Frappe app that connects **ERPNext** directly to
**bol.com**, the largest online marketplace in the Netherlands and Belgium, through the official
bol.com Retailer API. It is built for bol.com sellers who ship their own orders (Fulfilment by
Retailer, FBR) and want ERPNext to be the single place for stock, orders and accounting, without a
paid connector in between.

## bol.com koppeling voor ERPNext

Deze open-source Frappe-app koppelt ERPNext rechtstreeks aan bol.com via de officiële bol.com
Retailer API. Voor verkopers op bol.com in Nederland en België die zelf verzenden (FBR):

- aanbod, prijzen en voorraad automatisch gesynchroniseerd met bol.com;
- bestellingen van bol.com automatisch als verkooporder in ERPNext, met btw voor NL en BE (OSS);
- verzendbevestiging met track & trace, en verzendlabels via **Verzenden via bol (VVB)**;
- annuleringen en retouren verwerkt in ERPNext;
- verkoopfacturen en creditnota's, en de facturen van bol.com (commissie) als inkoopfactuur;
- export voor de bulk upload van nieuwe producten op bol.com.

## Features

- **Offer sync**: price (incl. VAT) from an ERPNext price list, delivery promise, economic operator
  and on-hold state for every Item sold on bol.com.
- **Stock sync**: sellable stock from one warehouse, minus a buffer, pushed to bol.com within a
  minute and reconciled nightly.
- **Order import**: every new bol.com order becomes a Sales Order, with NL and BE (OSS) tax templates.
- **Shipment confirmation**: submitting a Delivery Note confirms the shipment at bol.com, with
  track & trace (PostNL and other transporters).
- **Verzenden via bol (VVB)**: buy bol.com shipping labels from the Delivery Note and attach the PDF.
- **Cancellations and returns**: bol.com cancellations update the Sales Order; returns become return
  Delivery Notes, and the handling result is reported back to bol.com.
- **Accounting**: Sales Invoices and credit notes on a bol.com receivable, bol.com invoices
  (commission, labels) as draft Purchase Invoices, and invoice requests from business buyers
  uploaded automatically.
- **New products**: an export for bol.com's bulk upload of items not yet on bol.com.
- **Reliable by design**: idempotent imports, safe retries, and every failure in the integration log.

The app builds on `ecommerce_integrations` and reuses its `Ecommerce Item` and
`Ecommerce Integration Log`.

## Requirements

Frappe / ERPNext v15 and `ecommerce_integrations` (branch `version-15`).

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app --branch version-15 https://github.com/frappe/erpnext
bench get-app --branch version-15 https://github.com/frappe/ecommerce_integrations
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site $SITE install-app marketplace_integrations
```

## Documentation

How to use and operate the integration, in the order you set it up:

1. [Connect to bol](docs/usage/01-connect.md): credentials, logs, tokens
2. [Async operations (Bol Process)](docs/usage/02-processes.md)
3. [Catalog: which Items are sold on bol](docs/usage/03-catalog.md), adopting existing offers
4. [Offer sync](docs/usage/04-offer-sync.md)
5. [Stock sync](docs/usage/05-stock-sync.md)
6. [Order import](docs/usage/06-order-import.md)
7. [Shipments](docs/usage/07-shipments.md)
8. [Verzenden via bol labels](docs/usage/08-vvb-labels.md)
9. [Cancellations](docs/usage/09-cancellations.md)
10. [Returns](docs/usage/10-returns.md)
11. [Accounting](docs/usage/11-accounting.md)
12. [New products: bulk upload export](docs/usage/12-bulk-upload-export.md)

For developers: [using the bol client and process tracker](docs/usage/13-developers.md).
Design and engineering standards: [`docs/stories/00-overview.md`](docs/stories/00-overview.md).
Decisions and spec differences: [`docs/decisions.md`](docs/decisions.md).

## License

MIT
