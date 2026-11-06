# Costs

Each charge is priced when it is logged, and the price is stored with where it came from. Change a plan or a place's price and the charges it covers are priced again in the background.

## How a charge is priced

The first rule that applies wins:

1. **What you paid**, for a charge you entered by hand.
2. **What the network billed.** A Tesla Supercharger session takes the fees Tesla charged, read from Tesla's charging history, with a link to the invoice. The charge's page splits the bill into charging, idle (parking) and congestion fees, where the bill has them. This needs Connect Tesla ([sources](sources.md#tesla)) signed in with the charging scope. The history is read in the background and nothing about where the car has been is sent.
3. **The place's price**, set on Places or when you name a place: free, per kWh or per minute (with an optional session fee, and an optional idle price per minute after some free minutes), the home plan, or the regional average. A place called Work is not free until you say so.
4. **The home plan**, for a charge at home, by the hour the car drew power.
5. **Your fast-charging rates**, for a DC charge away from home: per kWh and per minute, for Superchargers and for other networks, with an optional fee per charge and an idle price per minute after some free minutes (Settings › Money).

**Idle fees.** For a charge no bill covers, idle minutes run from when the charge finished to when the car left. Finished is the end of the last stretch of the power profile drawing at least 1 kW. Left is the plug coming out where the source recorded it (the live connector's database plugin writes it when the cable disconnects), else the start of the next drive within three days (an upper bound: the cable may have come out earlier). The TeslaFi and TeslaMate imports end a session when charging stops and record no plug, so for them only a later drive gives an end. With no finish or no departure the idle time is unknown: no idle fee is charged, the charge page says "Idle unknown", and it is worked out once the car has left (re-checked for two days after the charge). Only a bill is exact. The fees are stored with the charge, are part of its cost, and so are in every total and stat. Charges priced before this existed show no split until they are priced again (changing a place's price or a fast-charging rate does that).
6. **The regional average**, for a home charge no plan covers: the average household price for its region and month, marked `≈` as an estimate.

A charge none of these prices shows as unpriced, not $0.

## The home plan

**Settings › Money** holds the home plan: periods and prices, a schedule by month, day and hour, and dated versions, so a charge from two years ago is priced at what was in force then.

**Find my plan** (US), **Settings › Money › Change plan**, has two ways in.

- **Your bill (PDF):** the most accurate. The bill is read on the server and never stored: only its rate schedule, its community-choice provider (MCE, SVCE and the rest), its start date and its prices per kWh are kept. Car Command finds the plan the bill names and prices it as the bill does, delivery and generation together, from the bill's first day on; earlier charges keep the plan they had. A bill covers one season, so the other season's prices are scaled from it and marked ≈ until a bill from that season is read.
- **Your utility:** pick it (your home's ZIP code suggests it, matched on your machine against NREL's [U.S. Electric Utility Companies and Rates: Look-up by Zipcode](https://data.openei.org/submissions/6225), CC BY 4.0), then a plan from OpenEI's [Utility Rate Database](https://openei.org/wiki/Utility_Rate_Database); Car Command imports every version of it, at the utility's published prices. A community-choice provider's prices are not in OpenEI: read them from your bill.

Each plan is shown with what it would have cost on your own home charging over the last year, beside the plan you have. Energy charges are priced at the first tier; fixed and demand charges are listed as a note. Picking a utility needs an OpenEI API key on that page, or `CC_URDB_URL` in `stack/.env` pointing at a relay that holds one. A request goes out only when you pick a utility or a plan, with only the utility or what you typed. The lists of US utilities (EIA Form 861), utilities by ZIP code (NREL) and California community-choice providers are bundled.

Outside the US, type your rates in.

## Regional averages

A file of average household electricity prices (US by state from the EIA, monthly from 2001; Europe by country from Eurostat, half-yearly) and petrol prices (EIA, EU Weekly Oil Bulletin) is bundled in the image, so an offline install works. When the copy is over 45 days old, or lacks a charge's region, a background worker downloads the current file, at most once a day. `CC_RATES_URL` points it at your own mirror.

The region comes from the charge's own position, matched against bundled country and state outlines on your box. Drive positions are not read for costs.

## Against a petrol car

Stats shows what a petrol car would have spent on the same miles: each charge's energy becomes miles at the car's measured efficiency, then fuel at the economy you set (30 mpg or 7.8 L/100 km when blank), at the average petrol price for that region and month, or a price you set.
