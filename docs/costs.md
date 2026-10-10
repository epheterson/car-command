# Costs

Each charge is priced when it is logged, and the price is stored with where it came from. Change a plan or a place's price and the charges it covers are priced again in the background.

## How a charge is priced

The first rule that applies wins:

1. **What you paid**, for a charge you entered by hand.
2. **What the network billed.** A Tesla Supercharger session takes the fees Tesla charged, read from Tesla's charging history, with a link to the invoice. This needs Connect Tesla ([sources](sources.md#tesla)) signed in with the charging scope. The history is read in the background and nothing about where the car has been is sent.
3. **The place's price**, set on Places or when you name a place: free, per kWh or per minute (with an optional session fee), the home plan, or the regional average. A place called Work is not free until you say so.
4. **The home plan**, for a charge at home, by the hour the car drew power.
5. **Your fast-charging rates**, for a DC charge away from home: per kWh and per minute, for Superchargers and for other networks.
6. **The regional average**, for a home charge no plan covers: the average household price for its region and month, marked `≈` as an estimate.

A charge none of these prices shows as unpriced, not $0.

## The home plan

**Settings › Money** holds the home plan: periods and prices, a schedule by month, day and hour, and dated versions, so a charge from two years ago is priced at what was in force then.

**Find my plan** (US): type your utility's name or a ZIP code and pick a plan from OpenEI's [Utility Rate Database](https://openei.org/wiki/Utility_Rate_Database); Car Command imports every version of it. Energy charges are priced at the first tier; fixed and demand charges are listed as a note. It needs an OpenEI API key on that page, or `CC_URDB_URL` in `stack/.env` pointing at a relay that holds one. A search goes out only when you press Search, with only what you typed. The list of US utilities (EIA Form 861) is bundled.

Outside the US, type your rates in.

## Regional averages

A file of average household electricity prices (US by state from the EIA, monthly from 2001; Europe by country from Eurostat, half-yearly) and petrol prices (EIA, EU Weekly Oil Bulletin) is bundled in the image, so an offline install works. When the copy is over 45 days old, or lacks a charge's region, a background worker downloads the current file, at most once a day. `CC_RATES_URL` points it at your own mirror.

The region comes from the charge's own position, matched against bundled country and state outlines on your box. Drive positions are not read for costs.

## Against a petrol car

Stats shows what a petrol car would have spent on the same miles: each charge's energy becomes miles at the car's measured efficiency, then fuel at the economy you set (30 mpg or 7.8 L/100 km when blank), at the average petrol price for that region and month, or a price you set.
