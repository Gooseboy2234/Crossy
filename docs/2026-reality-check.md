# Reality check: the runbook vs. Crossy Road as shipped (August 2026)

The runbook is written against the 2014 release. The game is now **v7.13**
(Hipster Whale Pty Ltd, last store update 2 Aug 2025, continuous content updates
through Jul 2026). Most of the runbook's *reasoning* survives. Several of its
*facts* do not, and two of them stop Phase 0 dead.

Ordered by what they cost you.

---

## 1. BLOCKING — lateral movement is a swipe, not a tap

**Tap = hop forward. Swipe left / right / down = sidestep and back.** There are
no tap zones for lateral movement.

This breaks the architecture, not just a constant:

- Phase 0's smoke test taps the centre five times and calls a PASS. That only
  proves Unity accepts synthetic **taps**. It says nothing about swipes, and a
  synthetic drag is a genuinely harder thing to get through `backboardd` into
  Unity's input layer than a tap is.
- The tap runner's wire protocol is a 4-byte `(x, y)` pair. It **cannot express
  a direction**. As specified, the bot can only ever move forward.
- `plan.py` emits `forward / left / right / wait`. Three of those four are
  unroutable.
- A forward-only bot cannot cross water at all — you must line up with a log.
  Water is where the runbook says all of `p` lives, so `p` would not be 1.5%,
  it would be "never finishes a river".

**Fix:** the protocol needs a gesture opcode, and Phase 0 has to prove swipes
work before anything else gets built. `XCUICoordinate.press(forDuration:thenDragTo:)`
with a short duration (~40-60ms) and ~15-20% of screen width of travel is the
call to test. If Unity ignores synthetic drags, the project is dead there — and
that is a much realer risk than the tap question the runbook was worried about.

Revised Phase 0 pass condition: **five forward hops, then a visible left step and
a visible right step.**

---

## 2. BLOCKING — wrong bundle ID

Runbook Phase 0 says `com.yesterday.crossyroad`. It is **`com.hipsterwhale.crossy`**.
`XCUIApplication(bundleIdentifier:)` fails on the wrong ID, so step 1 of step 1
fails. Confirm on-device with `ideviceinstaller -l` anyway.

Note also `com.hipsterwhale.crossyroadplus` — see §4.

---

## 3. Ads: worse than the runbook thinks, and fixable for a dollar

The runbook budgets 50-100 interstitials per overnight session and spends Phase 1
on DNS filtering to dodge them. Current reviews report an autoplay interstitial
roughly **every two runs**. At ~320 runs that is ~160 ads, not 50-100 — call it
75-90 minutes of an 8-hour night, plus the stall risk the runbook correctly
identifies as the most likely 2am failure.

But Hipster Whale sells the way out, which the runbook does not mention:

| Purchase | Effect |
|---|---|
| **Any** regular character (~$0.99) | Removes **autoplay ads** forever |
| **Ad Blocker Pack** (~$2.99) | Removes **all** ads forever, rewarded ads included, rewards still granted |

Autoplay interstitials are the entire problem — rewarded ads are opt-in and the
bot never opts in. So the cheapest character purchase deletes:

- the DNS filtering profile and its verification runs (Phase 1)
- the ad-interstitial state (§10.1) and its 45s force-relaunch fallback
- the single largest source of unattended stalls
- ~80 minutes per night, i.e. ~5-8% more attempts

This is the best hour-per-dollar in the project by a wide margin, and it does not
touch the "score must be earned by actually playing" property — it buys silence,
not points.

Keep a **timeout-and-relaunch fallback** on any unrecognised full-screen state
regardless. That guard is cheap and protects against every unknown, not just ads.

---

## 4. It is not one game any more — 28 worlds, and some change the rules

The store listing advertises **28 worlds**. These are not reskins:

| World | What changes |
|---|---|
| Original | cars, trucks, trains, rivers, logs — the runbook's model |
| Space | meteors/asteroids replace traffic; **UFOs replace the eagle** |
| Dinosaur | dinosaurs and crocodiles as obstacles; **pterodactyl replaces the eagle**; reworked Jul 2026 with new mechanics |

Runbook A.4 files "character selection may alter the biome" as an unexplored
lever worth a few manual runs. It is actually a first-class mechanic with 28
settings, and the wrong one silently invalidates every HSV threshold you
calibrated *and* the eagle model.

**Play the Original world.** Its palette and physics are the ones the whole
design assumes. A.4's suggestion to shop for a water-light biome is a real lever
but it trades a planning problem for a perception problem — take it only after
p̂ is measured and only with a fresh calibration pass.

---

## 5. Limited-time event modes can eat a whole night

The runbook's failure model is "stuck on an unrecognised menu". The 2026 game has
a worse one: **the main game mode is sometimes replaced.**

- **Hopside Down** (Apr 2026): the entire screen is flipped *except* the score,
  coin counter and pause button. Every ROI and the whole screen→world map invert.
- **Crashy Cart** (Nov-Dec 2025): hopping replaced by an auto-scrolling cart.

Either one running while you sleep is a total-loss night, and both look to the
harness like a catastrophic perception bug rather than a different game. Add an
explicit preflight assertion that the active mode is classic endless, and a
runtime sanity check (e.g. the chicken's screen position stays where calibration
says it is) that aborts to `debug/unknown/` instead of grinding.

---

## 6. Menu surface is far larger than §10.1's table

Beyond the runbook's six states, the current build has: **Pecking Order** (daily
global challenge, needs connectivity at run start and end), same-device
multiplayer, the gacha prize machine, a piggy bank, daily gifts, seasonal event
popups, and rate/upsell prompts. The state machine needs the unknown-state
dump-and-relaunch escape to be the *primary* mechanism, with named states as
optimisations — not the other way round.

---

## 7. Water has two carrier types

Rivers contain **logs and lily pads**. Different footprints and different visual
signatures. Perception that only models logs will misread lily-pad rows, and
water is exactly where you cannot afford that.

---

## 8. Trains announce themselves — use it

Track lanes have **flashing warning lights and a horn** before a train arrives.
This matters more than it sounds: a train crosses the chicken's footprint in
well under 50ms, which at a measured 60fps is under two frames. Tracking a
~20 col/s blob well enough to time a crossing is fragile; reading a large,
static, high-contrast warning light is trivial. **Classify the warning, don't
chase the train.**

---

## What survives unchanged

- Fixed **orthographic** camera, flat-shaded voxels, small palette → HSV +
  connected components is still the right call for the Original world. No CNN.
- **Game Center leaderboards and achievements are still supported** (current
  store listing). The premise holds.
- Core loop is unchanged: grid hops, score = rows advanced, idle → eagle.
- Score caps at 9,999, so a 328 target is nowhere near a ceiling.
- 60fps assumption: still the right default, still measure it.
- The whole `p`-driven framing, the p̂-not-mean-score objective, latency
  compensation, 3-frame median velocity, drain-to-newest-frame, water exit
  planning, and the kill-switch design are all unaffected by any of the above.

## What this means for the simulator

The sim models the Original world's physics — grass/road/water/track,
constant-velocity carriers, grid hops, an idle timer. That physics genuinely has
not changed since 2014, so the sim is not the part that is stale. What the above
does change:

1. add lily pads as a second carrier type,
2. generate the **Original** world's lane mix specifically, not a generic one,
3. the sim stays unvalidated until device p̂ exists — the runbook's own rule
   (within ~30% relative, else fix the noise model) is the gate.

None of that is worth doing before §1 is answered on real hardware. **There is no
point tuning `p` while three of the planner's four actions have no way to reach
the phone.**

---

*Sources: Apple App Store listing for Crossy Road (id924373886, v7.13) and
Crossy Road+ (id1559490508); hipsterwhale.com/crossy-road-support; crossyroad.com/updates;
Crossy Road Wiki (Content Updates, Pecking Order, Obstacles); BlueStacks and
Pocket Gamer play guides; App Store review sentiment 2025-26.*
