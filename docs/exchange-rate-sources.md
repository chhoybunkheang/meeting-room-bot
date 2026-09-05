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

## Corroborated rates for 2014-2021

The page now uses the following figures from the review. These are supported
by published financial reports, not independently verified original GDT/NBC
rate notices. The page explicitly displays that distinction and links to each
supporting report. No official publication date has been invented for them.

| Year | KHR per USD | Method | Supporting report |
| --- | ---: | --- | --- |
| 2014 | 4,038 | GDT annual rate reported in financial statements | [Phnom Penh Autonomous Port 2015](https://ppap.com.kh/wp-content/uploads/2024/11/Financial-Statement-FY-2015.pdf) |
| 2015 | 4,060 | GDT annual rate reported in financial statements | [Phnom Penh Autonomous Port 2015](https://ppap.com.kh/wp-content/uploads/2024/11/Financial-Statement-FY-2015.pdf) |
| 2016 | 4,037 | GDT annual average reported in financial statements | [Grand Twins 2017, note 4.2](https://www.acledasecurities.com.kh/as/assets/listed_company/GTI/FS-003-Financial%20Statements%202017.pdf) |
| 2017 | 4,045 | GDT annual average reported in financial statements | [Grand Twins 2017, note 4.2](https://www.acledasecurities.com.kh/as/assets/listed_company/GTI/FS-003-Financial%20Statements%202017.pdf) |
| 2018 | 4,045 | GDT annual average reported in financial statements | [Bank of China 2019, page 25](https://www.bankofchina.com.kh/dam/kh-kh/top/about-us/financial-report/annual-report/2019/annual-report-2019.pdf) |
| 2019 | 4,052 | GDT annual average reported in financial statements | [Bank of China 2019, page 25](https://www.bankofchina.com.kh/dam/kh-kh/top/about-us/financial-report/annual-report/2019/annual-report-2019.pdf) |
| 2020 | 4,045 | Year-end closing rate | [Equitable Cambodia 2021, note 2(c)](https://equitablecambodia.org/website/data/finance/EC-FS%202021%20-%20signed.pdf) |
| 2021 | 4,074 | Year-end closing rate | [Equitable Cambodia 2021, note 2(c)](https://equitablecambodia.org/website/data/finance/EC-FS%202021%20-%20signed.pdf) |

The year-end rule already applied from January 2020 under Instruction 27617
of 12 December 2019, as explained by
[KPMG's January 2020 update](https://assets.kpmg.com/content/dam/kpmg/kh/pdf/technical-update/2020/KPMG%20Tax%20Update%20-%20Jan%202020_The%20Law%20on%20Financial%20Management%20for%20year%202020.pdf).
The previous assumption that all pre-2022 years used annual averages was incorrect.
The financial-report evidence above does not independently establish all
filing-specific instructions for 2014-2019; original announcements remain to be checked.

## Data handling

- `annual_closing_rates` in `data/exchange_rate_updates.json` contains explicitly
  verified annual values, publication dates, source links, and verification dates.
  Add future years only after reviewing the corresponding official year-end
  publication. URL/date validation is a structural check, not source verification.
- For 2020 onward, neither a workbook annual row nor a December monthly row is
  accepted as proof of an annual TOI rate. A missing source leaves the annual
  value unavailable, including for completed years.
- `reported_annual_toi_rates` holds the corroborated 2014-2021 rates and their
  evidence level. Direct official closing-rate sources take precedence over
  financial-report evidence when both exist for a year.
- Years before 2014 remain historical reference values, not verified TOI
  filing rates. Historical averages are retained in the source JSON, but do not
  override sourced TOI figures or fill missing year-end rates from 2020 onward.
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
