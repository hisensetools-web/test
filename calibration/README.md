# Shop ID calibration

`shop_ids.csv` maps a Shopify shop ID to the date that store was created, for stores whose
creation date you have verified (for example in the Koala Inspector extension). Five or six rows
spread across a few years is enough; every other store's creation date is interpolated between
its two nearest rows and extrapolated at the ends.

Columns: `shop_id` (the number from `python tracker.py shop-ids`), `created_date` (YYYY-MM-DD).
`store_domain` and `notes` are for you; the tracker ignores them.

Example:

```
shop_id,created_date,store_domain,notes
23456789,2019-06-14,oldshop.com,Koala
58000000,2021-03-02,another.com,Koala
71234567,2023-01-20,third.com,Koala
88000000,2024-09-11,fourth.com,Koala
```
