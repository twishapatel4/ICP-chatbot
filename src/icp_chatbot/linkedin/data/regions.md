# Regions in countries.json

The seven level-1 rows of `countries.json` (North America, Latin America & Caribbean, Europe,
Middle East, Africa, Asia-Pacific, Other territories) are a **provisional grouping of the ISO
3166-1 countries into 7 regions defined by this project**. LinkedIn does not publish its
geography list (Sales Navigator offers continents and geographic divisions such as EMEA, APAC,
DACH, MENA). **These regions are not LinkedIn's published list.** Replace them with the TL's list
when available.

Shape: a region is a level-1 row (`id` `region-...`, no `parent`); each of the 249 ISO countries
is a level-2 row whose `parent` is its region's label. A consumer that needs ISO ids must filter
on `level == 2`.

`"pick": false` marks a row that is kept as data but not offered as a quick-pick. The options API
exposes it as `selectable: false`. Only "Other territories" (six countries, including Antarctica
`AQ`) carries it.
