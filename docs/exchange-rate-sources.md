# Exchange rate sources

Verified on 5 September 2026 against the public GDT December archives.
The values below are NBC official daily USD/KHR rates published by GDT.
Each archive was checked for its final published December observation.

| Fiscal year | KHR per USD | Publication date | Source |
| --- | ---: | --- | --- |
| 2022 | 4,117 | 2022-12-30 | [GDT December 2022](https://www.tax.gov.kh/en/exchange-rate?for_year=2022&for_month=12) |
| 2023 | 4,085 | 2023-12-29 | [GDT December 2023](https://www.tax.gov.kh/en/exchange-rate?for_year=2023&for_month=12) |
| 2024 | 4,025 | 2024-12-31 | [GDT December 2024](https://www.tax.gov.kh/en/exchange-rate?for_year=2024&for_month=12) |
| 2025 | 4,013 | 2025-12-31 | [GDT December 2025](https://www.tax.gov.kh/en/exchange-rate?for_year=2025&for_month=12) |

December 31 fell on a weekend in 2022 and 2023; the archives' last published
observations are December 30 and December 29 respectively. Preserve those
actual publication dates, rather than assigning a fictitious December 31 date.

[DFDL's explanation of Instruction 26118](https://www.dfdl.com/insights/legal-and-tax-updates/cambodia-official-exchange-rate-update/)
describes the December year-end official-rate rule for annual TOI declarations.
This is explanatory tax commentary; the numerical sources above are the GDT
archives themselves. The page's annual TOI rates apply to December year ends.

## Data handling

- `annual_closing_rates` in `data/exchange_rate_updates.json` contains explicitly
  verified annual values, publication dates, source links, and verification dates.
  Add future years only after reviewing the corresponding official year-end
  publication. URL/date validation is a structural check, not source verification.
- For 2022 onward, neither a workbook annual row nor a December monthly row is
  accepted as proof of an annual TOI rate. A missing source leaves the annual
  value unavailable, including for completed years.
- Pre-2022 annual averages remain historical reference values, not verified TOI
  filing rates. World Bank/IMF source metadata is kept with those overrides.
- Monthly coverage is calculated from the selected year's actual records. It is
  separate from the workbook file's modification time and the live daily feed.

## Daily-rate refresh

The public GDT daily feed is cached for six hours per application process.
Explicit refresh bypasses that cache after a shared 30-second cooldown. A failed
check retains the last successful rate, marks it as saved/stale, and records both
the last successful check and failed attempt. Ordinary requests retry failures
after ten minutes; explicit refresh can retry after the short cooldown.

Check times are displayed in Cambodian time (GMT+7). Publication dates remain
the dates supplied by GDT. Refresh updates the daily official-rate card without
resetting the selected year or implying that monthly records were downloaded.
The cache is in memory: process restarts clear it, and multiple workers maintain
independent caches and cooldowns.
