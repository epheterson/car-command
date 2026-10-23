# Roadmap

Planned after 0.1, roughly in order, with no dates. Each item ships when it works on real cars. Ideas and votes go to [Discussions › Ideas](https://github.com/epheterson/car-command/discussions/categories/ideas); a thumbs-up on an issue counts.

## Next: the Tesla stream in the one container

The Tesla live stream runs only under Docker Compose today. The receiver and its Tailscale Funnel move into the one container, switched on by a Tailscale auth key, so a Tesla owner gets live drives and charges without a second stack.

## Then: commands

Commands to the car, in three tiers, so the owner decides how much the site can do.

| Tier | Commands | What it takes |
|---|---|---|
| 1, on by default | start and stop charging, charge limit and amps, climate on and off with a temperature, seat and defrost presets, honk and flash, wake, lock | signed in, and a confirm tap |
| 2, off until turned on per car | Sentry or Halo Secure, scheduled charging and departure | tier 1, and switched on for that car |
| 3, off until turned on, asked each time | unlock, trunk and frunk, windows, remote start | tier 2, and the password within the last five minutes |

Not planned: anything that moves the car, software updates, PIN or speed-limit changes, key management.

- **Tesla:** signed commands with the virtual key Connect Tesla already paired.
- **Lucid:** through the CarConnectivity connector, which already holds the token.
- **Powerwall:** backup reserve and mode.
- Every command is logged and shown on the car's page; tier 2 and 3 also send an alert. Commands are rate limited, POST-only with a CSRF token, and one switch turns them all off. A new install starts with commands off.

## Wall-side meters

What the charger delivered, read from the charger or the panel: Tesla Wall Connector Gen 3, Emporia, Sense. With both sides, a charge shows its losses.

## Local map backgrounds

Map backgrounds from the local maps add-on, so maps have streets with public tiles off.

## Also wanted

- More makes through CarConnectivity, tested by people who own them. If yours works, say so in [Show and tell](https://github.com/epheterson/car-command/discussions/categories/show-and-tell); if not, open an issue with the `source: carconnectivity` label.
- A rate plan per place (work, a second home).

## Not planned

- A hosted version. Car Command runs on your hardware, with no account to sign up for.
- Comparisons across other people's cars.
