# API findings

Tested against a live Wise account (UK personal profile) on 2 October 2026, cross-checked with the Wise Platform API reference (version 2026Q4).

## Works with a UK personal API token

| Call | Notes |
|---|---|
| `GET /2026Q4/profiles` | Profile `type` is `PERSONAL` (uppercase) |
| `POST /2026Q4/profiles/{id}/quotes` | NGN amount is inside `paymentOptions`; top-level `targetAmount` came back empty |
| `GET /2026Q4/quotes/{quoteId}/account-requirements` | Returns NGN recipient types `nigeria` and `email` |
| `POST /2026Q4/accounts` | NGN: type `nigeria`, details `legalType`, `bankCode`, `accountNumber`. OPay is bank code `100004` |
| `POST /2026Q4/transfers` | `customerTransactionId` must be a UUID |
| `GET /2026Q4/transfers`, `PUT /2026Q4/transfers/{id}/cancel` | Listing and cancelling unpaid transfers |
| `GET /2026Q4/rates` | Supports `from`, `to`, `group=hour`. 30 days returned 720 hourly rows. Matched the quote rate exactly when measured in the same minute |

## Does not work

| Call | Result |
|---|---|
| `POST /2026Q4/profiles/{id}/transfers/{id}/payments` | HTTP 403 `{"error": "forbidden"}`. Wise's docs state funding via personal tokens is only supported for accounts based in the US, CA, AU, NZ, SG and MY. UK and EEA accounts are subject to PSD2 Strong Customer Authentication |

## Behaviour worth knowing

- `BALANCE` is only offered as a pay-in option when the funds are already in the Wise balance. Otherwise the enabled options were card-style methods such as `GOOGLE_PAY`.
- Fees have a large fixed part: £0.57 on £2 (28%).

## Not verified

- The maximum date range per `/rates` history call (backfill uses 7-day chunks)
