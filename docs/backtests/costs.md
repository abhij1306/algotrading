# Backtest costs: the dated schedule and its sources

A fill is charged under the file of its cost-model version in force on its trade date
(`backend/app/hat/cost_models/<version>/<effective_from>.yaml`, `costs.charges`); a run records
the digest of every file in force in its window. The default is `cnc-delivery-2`;
`cnc-delivery-1` stays packaged so its runs can be rerun (it had a lower pre-2020 stamp duty and
SEBI fee, no IPFT, GST on the SEBI fee throughout and today's DP charge throughout). The
hand-worked charges of a buy and a sell per period are the tests of `charges`
(`tests/hat/properties/test_costs.py`, `test_charges_hand_worked`).

Rates are of the fill's value (qty x price) per side; each component is rounded to the paisa,
GST (service tax before 2017-07-01) is charged on the taxable components' sum. Brokerage is 0
(Fyers delivery) and the clearing charge is 0 to the client.

| From | STT (buy, sell) | Stamp duty | NSE txn | IPFT | SEBI fee | Tax on txn (+SEBI, IPFT) | DP per sell day |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2011-06-22 | 0.125%, 0.125% | 0.01% both sides | 0.00325% | - | Rs 20/cr, untaxed | 10.30% | Rs 17.10 |
| 2012-04-01 | | | | | | 12.36% | Rs 17.42 |
| 2012-07-01 | 0.1%, 0.1% | | | | | | |
| 2015-06-01 | | | | | | 14% | Rs 17.67 |
| 2015-11-15 | | | | | | 14.5% | Rs 17.75 |
| 2016-06-01 | | | | | | 15% | Rs 17.83 |
| 2017-04-01 | | | | | Rs 15/cr | | |
| 2017-07-01 | | | | | | 18% GST | Rs 18.29 |
| 2019-04-01 | | | | | Rs 10/cr | | |
| 2020-06-01 | | | | | Rs 5/cr | | |
| 2020-07-01 | | 0.015% on buys | | | | | |
| 2021-01-01 | | | 0.00345% | | | | |
| 2021-04-01 | | | | | Rs 10/cr | | |
| 2021-08-05 | | | | | | | Rs 14.75 |
| 2022-07-18 | | | | | GST applies | | |
| 2023-04-01 | | | 0.00325% | Rs 10/cr, GST | | | |
| 2024-04-01 | | | 0.00322% | | | | |
| 2024-10-01 | | | 0.00297% | | | | |

A blank cell carries the row above.

**Sources.**

- STT on delivery, buys and sells, Finance (No. 2) Act 2004 s.98: 0.125% to 2012-06-30, 0.1%
  from 2012-07-01 (Finance Act 2012; NSE/FATAX/20990), unchanged since.
- Stamp duty before 2020-07-01 was a state levy on the broker's contract note. HAT charges the
  Maharashtra rate (NSE's clearing is in Mumbai): Maharashtra Stamp Act Sch. I Art. 51A(b)(i)
  (Mah. Act 32 of 2005), Re 1 per Rs 10,000 (0.01%) of a delivery trade, both sides. The 0.005%
  rate of the Maharashtra Act 15 of 2011 was never brought into force. From 2020-07-01 the
  uniform 0.015% on delivery buys, collected by the exchange (Indian Stamp Act as amended by the
  Finance Act 2019).
- NSE transaction charges: 0.00325%, the first slab from 2009-10-01 (NSE/F&A/13028,
  2009-09-07; Rs 3.50 per lakh before), 0.00345% from 2021-01-01 (NSE/FA/46730, 2020-12-18),
  0.00325% from 2023-04-01 (NSE/FA/56129, 2023-03-24),
  0.00322% from 2024-04-01 (NSE/FA/61137, 2024-03-14), a flat 0.00297% from 2024-10-01
  (NSE/FA/64232, 2024-09-27, under SEBI/HO/MRD/TPD-1/P/CIR/2024/92).
- IPFT (investor protection fund trust) contribution Rs 10 per crore each side from 2023-04-01,
  GST on it (NSE/FA/56129).
- SEBI turnover fee: Rs 20 per crore (0.0002%, SEBI (Stock Brokers and Sub-brokers) (Third
  Amendment) Regulations 2006 as amended 2006-09-25) to 2017-03-31 (SEBI PR 5/2017), Rs 15
  from 2017-04-01 (SEBI/LAD-NRO/GN/2016-17/038), Rs 10 from 2019-04-01 (SEBI/LAD-NRO/GN/2019/03;
  NSE/FA/40632),
  Rs 5 from 2020-06-01 to 2021-03-31 (SEBI PR 24/2020; SEBI/LAD-NRO/GN/2020/011; NSE/FA/44514).
  GST on it only from 2022-07-18 (Notification 04/2022-Central Tax (Rate); SEBI circular
  2022/0097); exempt before.
- Service tax with its 3% education cesses: 10.30% (10%, Notification 8/2009-ST) to 2012-03-31,
  12.36% (12%, Finance Act 2012) from 2012-04-01; 14%, cesses subsumed, from 2015-06-01
  (Finance Act 2015, Notification 14/2015-ST); + Swachh Bharat cess 0.5% from 2015-11-15
  (Finance Act 2015 s.119, Notification 21/2015-ST); + Krishi Kalyan cess 0.5% from 2016-06-01
  (Finance Act 2016 s.161); GST 18% from 2017-07-01 (CGST/SGST/IGST Acts 2017).
- DP charge (Fyers, per scrip per sell day, tax included): Rs 15.50 + tax before 2021-08-05,
  Rs 12.50 + 18% GST after (Fyers support, "What are the DP charges at Fyers"; fyers.in charges
  list). Fyers' tariff before 2021 is not published; the Rs 15.50 base is used from 2011-06-22 on
  with the tax in force.
