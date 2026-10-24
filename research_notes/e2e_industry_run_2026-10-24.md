# Live end-to-end run: 24 industries, aligned and non-aligned users (2026-10-24)

Each industry: one real company website (scraped by the bot), one **aligned** simulated user and one **non-aligned** one (tangent, vague, refuser, chatty/off-topic, contradictory, impatient). A second model played the user, using the scraped page text and its own knowledge of the company. Run through the real API against the local Postgres `icp_chatbot` on the DB v2 models (3.1-flash-lite for the interview). Everything is stored: tenants `e2e-aligned-2026-10-24` and `e2e-nonaligned-2026-10-24` hold 48 sessions, 310 turns, 48 enrichment runs, 41 ICP versions and one `e2e_eval` feedback event per run (`field` = industry). Judge = a model grading strictly against the website excerpt and transcript; its excerpt is cut to 1,500 characters, so its "hallucinated" flag over-reports industries that came from other scraped pages.

Query: `select * from feedback_events where type='e2e_eval'`.

| Industry | Site | Mode | Behaviour | User turns | End | ICP | Core fields known | Accuracy | Complete | Hallucinated |
|---|---|---|---|---|---|---|---|---|---|---|
| agriculture | John Deere | aligned | aligned | 8 | completed/complete | built | 79% | - | - | - |
| agriculture | John Deere | nonaligned | contradictory | 9 | completed/complete | built | 29% | 2 | 2 | False |
| automotive | CDK Global | aligned | aligned | 5 | completed/complete | built | 36% | 5 | 5 | False |
| automotive | CDK Global | nonaligned | chatty_offtopic | 2 | fallback/off_topic | not built | -% | 0 | 0 | False |
| b2b_saas | Basecamp | aligned | aligned | 3 | completed/complete | built | 33% | 4 | 5 | True |
| b2b_saas | Basecamp | nonaligned | tangent | 4 | completed/complete | built | 50% | 5 | 5 | False |
| biotech_pharma | Benchling | aligned | aligned | 6 | completed/complete | built | 58% | 4 | 5 | True |
| biotech_pharma | Benchling | nonaligned | impatient | 6 | completed/user_finished | built | 50% | 4 | 5 | True |
| construction | Procore | aligned | aligned | 7 | completed/complete | built | 46% | 5 | 5 | False |
| construction | Procore | nonaligned | impatient | 5 | completed/complete | built | 29% | 5 | 5 | False |
| cybersecurity | SentinelOne | aligned | aligned | 6 | completed/user_finished | built | 54% | 4 | 5 | True |
| cybersecurity | SentinelOne | nonaligned | tangent | 9 | completed/complete | built | 85% | 5 | 5 | False |
| ecommerce_retail | Allbirds | aligned | aligned | 4 | completed/complete | built | 86% | 5 | 5 | False |
| ecommerce_retail | Allbirds | nonaligned | chatty_offtopic | 3 | fallback/off_topic | not built | -% | 0 | 0 | False |
| edtech_education | Docebo | aligned | aligned | 6 | completed/complete | built | 54% | 4 | 5 | True |
| edtech_education | Docebo | nonaligned | vague | 7 | completed/complete | built | 9% | 3 | 3 | True |
| energy_solar | Aurora Solar | aligned | aligned | 4 | completed/complete | built | 54% | 5 | 5 | False |
| energy_solar | Aurora Solar | nonaligned | tangent | 6 | completed/complete | built | 58% | 5 | 5 | False |
| fintech_payments | Mercury | aligned | aligned | 4 | completed/complete | built | 45% | 5 | 5 | False |
| fintech_payments | Mercury | nonaligned | vague | 10 | completed/user_finished | not built | -% | 0 | 0 | False |
| healthcare_dental | Tebra | aligned | aligned | 8 | completed/complete | built | 67% | 4 | 5 | True |
| healthcare_dental | Tebra | nonaligned | refuser | 11 | completed/complete | built | 46% | - | - | - |
| hr_recruiting | Greenhouse | aligned | aligned | 6 | completed/complete | built | 69% | 5 | 5 | False |
| hr_recruiting | Greenhouse | nonaligned | vague | 9 | completed/user_finished | not built | -% | 0 | 0 | False |
| insurance | Guidewire | aligned | aligned | 6 | completed/complete | built | 69% | 5 | 5 | False |
| insurance | Guidewire | nonaligned | refuser | 7 | completed/complete | built | 36% | 4 | 4 | True |
| iot_telecom | Samsara | aligned | aligned | 6 | completed/complete | built | 62% | 5 | 5 | False |
| iot_telecom | Samsara | nonaligned | impatient | 5 | completed/complete | built | 38% | 5 | 5 | False |
| it_consulting | Thoughtworks | aligned | aligned | 6 | completed/complete | built | 83% | 5 | 5 | False |
| it_consulting | Thoughtworks | nonaligned | contradictory | 4 | completed/complete | built | 73% | 4 | 4 | True |
| legal_services | MyCase | aligned | aligned | 6 | completed/complete | built | 50% | 5 | 5 | False |
| legal_services | MyCase | nonaligned | refuser | 6 | completed/complete | built | 67% | 5 | 5 | False |
| logistics_freight | ShipBob | aligned | aligned | 7 | completed/complete | built | 71% | - | - | - |
| logistics_freight | ShipBob | nonaligned | impatient | 6 | completed/user_finished | built | 38% | 4 | 4 | True |
| manufacturing | Sandvik | aligned | aligned | 8 | completed/complete | built | 77% | 5 | 5 | False |
| manufacturing | Sandvik | nonaligned | contradictory | 12 | completed/complete | built | 33% | 1 | 1 | True |
| marketing_agency | Single Grain | aligned | aligned | 9 | completed/complete | built | 85% | 5 | 5 | False |
| marketing_agency | Single Grain | nonaligned | chatty_offtopic | 4 | fallback/off_topic | not built | -% | 0 | 0 | False |
| media_entertainment | Spotify | aligned | aligned | 7 | completed/complete | built | 100% | 5 | 5 | False |
| media_entertainment | Spotify | nonaligned | contradictory | 12 | completed/forced_finish | not built | -% | 0 | 0 | False |
| nonprofit | Classy | aligned | aligned | 6 | completed/complete | built | 82% | 5 | 5 | False |
| nonprofit | Classy | nonaligned | chatty_offtopic | 6 | fallback/off_topic | not built | -% | 0 | 0 | False |
| real_estate_proptech | AppFolio | aligned | aligned | 5 | completed/complete | built | 33% | 5 | 5 | False |
| real_estate_proptech | AppFolio | nonaligned | tangent | 5 | completed/complete | built | 27% | 5 | 5 | False |
| restaurants_food | Olo | aligned | aligned | 6 | completed/complete | built | 73% | 5 | 5 | False |
| restaurants_food | Olo | nonaligned | vague | 9 | completed/complete | built | 9% | - | - | - |
| travel_hospitality | Mews | aligned | aligned | 6 | completed/complete | built | 58% | 5 | 5 | False |
| travel_hospitality | Mews | nonaligned | refuser | 8 | completed/complete | built | 58% | 5 | 4 | False |
