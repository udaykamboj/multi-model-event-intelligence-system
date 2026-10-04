# Design Infrastructure Platform

## You

We want our system to be based around the user, right? So what our current system is based around is protests, right? So protests have a lot of different, like, things in relation to them, right? So you have to understand we're trying to talk about infrastructure impact, users, like citizen impact, protests, right? Not even in the bad way. I'm talking about, like, in ways, how could it impact them, right? Let's say day to day. Like currently it's based around protests. Later it could be any infrastructure-related issues, earthquakes, blah, blah, blah. But more or less, the validity is based around when you have different protests, you have different characteristics of them, right? You have, like, so historically different protests have had different impacts, right? We have historical purposes. We have purposes, regional impacts of protests. We have a lot of these impacts of, right? So the system needs to go ahead and have insane datasets of that. The system itself needs to be able to— the system itself needs to be able to utilize, like current infrastructure things, current reports. So they should have data. They should have immense data, and it should be able to pick it up in real time and have past historical data, right? So whenever any, like... so you know how, like, government systems track when anything is reported? If a protest is being reported, blah, blah, blah, the government system should be able to track that. And if you can find a system that's publicly available, even if it's not government, that's tracking protests— let's do greater Pacific area, the Washington Greater Pacific Sound area— be able to go ahead and pick up active reports of when stuff has happened, right? Active reports, currently active. Like if something's happening within the next ten minutes, it should be able to pick that up, right? Pick up this data, and then we should run predictive analysis, right? We should be, like, at different scales what's happening with it, should be able to notify the users if stuff has been escalated, right? It should be able to go ahead and, let's say... let's say, like, for example, you're in— if, like, let's say a terrorist attack happens within the protest, or, like, they're ravaging places, like if traffic routes were to avoid being, right? If you want to be, let's say, evacuated, where to evacuate to, right? Based around this. There should be, like, one guy was talking about three-level system, where first is just, like, an over, like, informative aspect of what's happening where, right? Second layer, like, where is this impact happening, and what is the problem related to this impact? And then the third layer should be like, if I need to evacuate, what do I need to do? Blah, blah, blah, in direct relation what I need to do. So the system needs to be based around, again, real-time data on what's happening, news articles, blah, blah, blah. It should be able to pick up this data and should be able to utilize it. It should be based around current analysis of previous systems, like of previous events, and it should be able to go ahead and use all these things to come up with, like, we need to come up with variables, like evacuation variables, this variable, that variable, because each individual event has its own characteristics, right? If we're able to understand what variables are the most important, right? We should be able to create a system that can utilize that. So that's one thing that you need to go ahead and make: make these variables, make these algorithms. What if there's, like, 30 or 40 things that we need to take into account? What are those things? And when you're making an analysis and it shows people, what are the 30 and 40 things that's going to need to be able to show and show? Like let's say, like, analyze 30 things, right? The most impactful ones. Like if there's a really high, like, risk of a certain thing happening, then we only show, like, the top ones, right, within the user interface. Something like that. Could you plan that all out?

## ChatGPT

Yes. I would build this as an **infrastructure-impact intelligence platform**, with protests as the first event type—not as a “protest tracker.”

The central object is:

> **Event → Infrastructure impact → Geographic exposure → Individual impact → Recommended action**

That architecture lets you later swap in earthquakes, wildfires, severe weather, major accidents, utility failures, transit disruptions, or other infrastructure events without rebuilding the whole system.

## 1. The actual product

Think of the system as having three levels.

### Level 1 — “What is happening?”

A live regional situation layer.

For every detected event, the system creates an **Event Card**:

```text
EVENT
────────────────────────
Demonstration
Downtown Seattle
Started: 3:42 PM
Last updated: 3:51 PM

Confidence: HIGH
Status: ACTIVE

Reported characteristics:
• Estimated geographic area
• Estimated crowd size
• Roadway involvement
• Transit involvement
• Police/emergency response
• Reported injuries/damage
• Direction/location changes
• Planned duration, if known
```

Crucially, the system should **separate confirmed information from inference**.

For example:

```text
CONFIRMED
SDOT reports road closure.

REPORTED
Local news reports demonstration moved toward...

INFERRED
Transit disruption likely to expand northward.

PREDICTED
Probability of additional route disruption: ...
```

That prevents an AI-generated interpretation from accidentally becoming presented as fact.

---

# 2. Level 2 — “How does this affect me?”

This is where your system becomes much more interesting.

The user isn't really asking:

> “What is happening with the protest?”

They're asking:

> **“I'm at/going to X. What does this mean for me?”**

So every event gets converted into an **Impact Graph**.

For example:

```text
                 EVENT
                   │
        ┌──────────┼──────────┐
        ↓          ↓          ↓
      Roads      Transit    Public
                            facilities
        │          │          │
        ↓          ↓          ↓
   Route delay   Station     School
                 closure     access
        │
        ↓
   User's trip
        │
        ↓
   PERSONAL IMPACT
```

You could therefore have:

### Regional impact

> “Downtown Seattle transportation disruption.”

### Local impact

> “3 major roads within 0.7 miles of you are affected.”

### Personal impact

> “Your route to school intersects the affected corridor.”

That is the **user-centered part**.

---

# 3. Level 3 — “What should I do?”

This shouldn't be generic AI advice.

It should be a **decision-support engine based on verified conditions**.

For example:

```text
YOUR SITUATION

You are currently:
📍 0.8 mi from affected area

Your planned destination:
🏫 School

Current route:
🚗 Route 1

────────────────────

IMPACT

HIGH
Route 1 intersects an active closure.

Transit:
MEDIUM
Two nearby routes have service disruption.

────────────────────

ACTION

Your current route is affected.

Alternative:
Route 2

Expected additional travel:
~12 minutes

If you are already inside the affected area:
follow official emergency instructions.
```

For an actual emergency, the system should defer to official evacuation/emergency instructions rather than inventing an evacuation destination.

Seattle's existing AlertSeattle system is a useful model here: it already supports geographically targeted emergency notifications and covers safety, transportation disruptions, utility disruptions and demonstrations. 

---

# 4. Your data architecture

This is probably the biggest part of the project.

You don't want one giant dataset.

You want a **multi-source event intelligence layer**.

### A. Historical event data

This gives you the training/analysis foundation.

For protests/demonstrations, ACLED is particularly useful because its US data records event date, actors, location and event type, and distinguishes things such as peaceful protests, protests with intervention, excessive force against protesters and violent demonstrations. 

It also provides API/export access and historical/aggregated data. 

Your historical database could therefore look like:

```text
Event
├── Date/time
├── Location
├── Event type
├── Duration
├── Participants/crowd estimate
├── Movement pattern
├── Roads affected
├── Transit affected
├── Police/emergency response
├── Reported injuries
├── Property/infrastructure impacts
├── Closures
├── Geographic spread
├── Weather
├── Time of day
├── Day of week
├── Nearby infrastructure
├── Population exposure
├── Historical analogues
└── Outcome
```

And importantly, you **don't need to make the system about political ideology or which side a protest represents**. Those can be contextual fields where genuinely relevant, but the model should focus on observable characteristics and infrastructure consequences.

---

# 5. Real-time data

This is where the Seattle/Puget Sound version gets really strong.

### Transportation

WSDOT has a Traveler Information API with highway alerts, cameras and traffic information. Its traffic-flow service covers major Washington regions and is generally updated around every 90 seconds. 

WSDOT's highway-alert endpoint specifically exposes **currently active incidents**. 

That's exactly the kind of feed your system needs.

### Seattle transportation

Seattle also has a Right-of-Way Impacts API designed around current traffic incidents and construction events obstructing public right-of-way. 

### Transit

King County Metro provides GTFS-RT feeds for:

- trip updates
- vehicle positions
- service alerts

and its data covers Metro, Seattle Streetcar, Water Taxi, Sound Transit Link and some regional services. 

Sound Transit separately provides real-time information and service-alert feeds. 

So your system could literally understand:

```text
EVENT
 ↓
Road closure
 ↓
Traffic congestion
 ↓
Bus route affected
 ↓
Train/bus alternatives
 ↓
User's trip affected
```

---

# 6. Emergency/event feeds

You can also ingest official emergency information.

Seattle Fire has a real-time 911 dispatch page that automatically refreshes every 60 seconds. 

Seattle Police publishes Significant Incident Reports, although it explicitly warns that these are based on early information and may differ from final reports. 

That's actually useful for your **confidence system**.

Instead of saying:

> “There is definitely a major incident.”

you can say:

> **Early report — confidence medium**

until multiple sources corroborate it.

---

# 7. Then add news

News shouldn't be your primary truth source.

It should be a **signal layer**.

Architecture:

```text
                 ┌── Government feeds
                 ├── Transportation
                 ├── Transit
EVENT DETECTION ─┼── Emergency dispatch
                 ├── Weather
                 ├── News
                 └── Public reports
                         │
                         ↓
                  EVENT RESOLUTION
                         │
                  ┌──────┴──────┐
                  ↓             ↓
             Confidence      Timeline
                  │             │
                  └──────┬──────┘
                         ↓
                  IMPACT ENGINE
```

If three independent sources report essentially the same event, your confidence rises.

If one social/news report says something but no official source confirms it:

```text
Status: UNVERIFIED
Confidence: LOW
```

That distinction is extremely important.

---

# 8. The 40 variables

This is where I'd make your research component substantially deeper.

Don't just throw 40 variables into an ML model.

Create **variable families**.

## Event variables

1. Event type  
2. Event start time  
3. Event duration  
4. Event geographic footprint  
5. Event movement direction  
6. Event growth rate  
7. Estimated participation  
8. Participation density  
9. Event organization/structure  
10. Number of simultaneous locations  

## Infrastructure variables

11. Roads affected  
12. Road classification  
13. Number of lanes affected  
14. Closure severity  
15. Bridges/tunnels affected  
16. Transit routes affected  
17. Transit stations/stops affected  
18. Transit frequency  
19. Utility infrastructure proximity  
20. Critical facilities nearby  

## Transportation variables

21. Current traffic volume  
22. Current congestion  
23. Historical congestion baseline  
24. Route redundancy  
25. Alternative-route availability  
26. Travel-time increase  
27. Transit capacity  
28. Number of affected routes  
29. Pedestrian accessibility  
30. Geographic bottlenecks  

## Environmental/context variables

31. Weather  
32. Visibility  
33. Time of day  
34. Day of week  
35. School/work activity  
36. Major scheduled events  
37. Population density  
38. Historical event similarity  
39. Source confidence  
40. Rate of change  

And I'd add a few **derived variables**:

41. Exposure score  
42. Infrastructure dependency score  
43. Disruption propagation score  
44. User proximity  
45. User-route intersection  
46. Alternative availability  
47. Information confidence  
48. Escalation/change score  

Those derived variables are actually more useful to the user than many raw variables.

---

# 9. Don't show all 40 variables to the user

This is an important design decision.

The backend might evaluate 40–50 variables.

The interface should probably show **3–6 reasons**.

For example:

### Why is this HIGH impact?

```text
HIGH IMPACT

3 factors are driving the assessment:

🚧 Major road closure
   2 primary routes affected

🚌 Transit disruption
   4 routes reporting delays

📍 Proximity
   Your destination is 0.6 mi from
   the affected corridor
```

Then:

> **View all 42 analyzed factors**

could open the technical explanation.

That gives you both usability **and research transparency**.

---

# 10. The actual scoring architecture

I'd avoid a single mysterious:

> Risk = 83%

Instead:

### Event Severity

How serious is the event itself?

### Infrastructure Disruption

How much infrastructure is actually affected?

### Geographic Exposure

How many people/locations are within the affected area?

### Propagation Potential

How likely is the disruption to spread through connected infrastructure?

### User Exposure

How directly does it affect this specific person?

### Confidence

How reliable is the underlying information?

Then:

```text
                  EVENT
                    │
        ┌───────────┼───────────┐
        ↓           ↓           ↓
     Severity   Infrastructure  Confidence
        │           │
        └─────┬─────┘
              ↓
       Geographic Impact
              │
              ↓
       User Exposure
              │
              ↓
       Recommended Action
```

This is much more defensible than an unexplained black-box score.

---

# 11. Historical similarity is where the ML gets interesting

Suppose today's event looks like:

```text
Downtown
+
large gathering
+
weekday
+
rush hour
+
two arterial roads
+
one transit corridor
+
rapid geographic expansion
```

The system searches historical events with similar characteristics.

It could discover:

```text
Historical analogues

Event A — similarity 87%
Event B — similarity 82%
Event C — similarity 78%
```

Then determine:

> In historically similar situations, what infrastructure impacts followed?

For example:

```text
Historical pattern:

Road closure
       ↓
Congestion increase
       ↓
Bus delays
       ↓
Secondary route congestion
```

This is much more scientifically interesting than simply predicting:

> “A protest will become violent.”

Your system is fundamentally predicting **infrastructure consequences**, which is both more aligned with the user problem and more measurable.

---

# 12. The system should learn different event → impact relationships

This is probably the strongest research angle.

You aren't asking:

> “What happens at protests?”

You're asking:

> **“Which observable characteristics of an event best explain subsequent infrastructure disruption?”**

That gives you an actual research question.

For example:

```text
                   Characteristics
                         │
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
     Location          Timing          Movement
        │                │                │
        └────────────────┼────────────────┘
                         ↓
                 Infrastructure
                    exposure
                         ↓
                  Actual outcome
```

Then you can use:

- feature importance
- SHAP
- temporal models
- spatial models
- gradient boosting
- graph-based models
- time-series forecasting
- survival/time-to-impact analysis

depending on how ambitious you want the research to become.

---

# 13. Infrastructure should eventually become a graph

This is the part I'd really emphasize.

Represent the city as:

```text
ROAD A ─── ROAD B ─── BRIDGE
  │          │
  │          └── BUS ROUTE 1
  │
  └── STATION
       │
       └── TRAIN LINE
```

Then an event doesn't merely affect a point.

It affects **connected infrastructure**.

Example:

```text
Event
 ↓
Road closure
 ↓
Traffic reroutes
 ↓
Road B congestion
 ↓
Bus Route 1 delay
 ↓
Station crowding
 ↓
User trip disruption
```

That's a much more sophisticated system than an event map.

---

# 14. User model

The user should have a small personal context model.

Not invasive surveillance—just information the user intentionally provides.

For example:

```text
USER
├── Current/selected location
├── Destination
├── Preferred transportation
├── Normal commute
├── Saved locations
└── Notification preferences
```

Then your engine asks:

> Which infrastructure nodes does this user depend upon?

For one person:

```text
HOME → BUS → TRAIN → SCHOOL
```

For another:

```text
HOME → I-5 → WORK
```

Same event.

**Completely different impact.**

That's what makes it genuinely user-centered.

---

# 15. Notifications should be change-driven

Don't notify someone every time the database changes.

Notify when something **meaningfully changes**.

For example:

```text
EVENT CREATED
       ↓
Low impact
       ↓
No notification

       ↓

Road closure detected
       ↓
Medium impact
       ↓
Notify affected users

       ↓

User's route now blocked
       ↓
High personal impact
       ↓
Immediate notification
```

Your notification engine can have:

```text
NEW EVENT
STATUS CHANGE
LOCATION CHANGE
IMPACT CHANGE
ROUTE CHANGE
ESCALATION
RESOLUTION
```

That is much more useful than constant alerts.

---

# 16. Your UI could look like this

### Home

```text
GOOD AFTERNOON

Your area
Seattle, WA

────────────────────────

ACTIVE IMPACTS

🔴 High
Downtown transportation disruption

🟡 Moderate
I-5 congestion

🟢 Low
Minor road closure

────────────────────────

YOUR ROUTES

🏫 School
⚠ Route affected

🏠 Home
✓ No significant impact
```

Then clicking the event:

```text
DOWNTOWN DISRUPTION

ACTIVE
Updated 2 min ago

WHAT'S HAPPENING
Demonstration affecting downtown
transportation.

IMPACT
████████░░ HIGH

WHAT IS AFFECTED
🚧 4 roads
🚌 6 transit routes
🚶 pedestrian access
🚗 congestion

WHY
• Major arterial affected
• Event expanding geographically
• Rush-hour traffic
• Limited alternate routes

YOUR IMPACT
Your saved route to school
intersects the affected area.

ACTION
⚠ Current route affected

Alternative route available
```

And then:

**“Why?”**

opens the evidence.

---

# 17. Evidence panel

This is critical for credibility.

```text
WHY WE THINK THIS

Official sources
✓ WSDOT traffic alert
✓ Metro service alert
✓ Seattle road-impact feed

Independent reporting
✓ News report A
✓ News report B

Historical comparison
✓ 8 similar events identified

Model assessment
Infrastructure disruption: HIGH
Confidence: 91%
```

The user can therefore distinguish:

**data → analysis → recommendation**

rather than believing the AI blindly.

---

# 18. Then generalize beyond protests

Your database should use a universal:

```text
EVENT
```

object.

Then:

```text
Event Type
├── Demonstration
├── Earthquake
├── Wildfire
├── Severe weather
├── Flood
├── Landslide
├── Major collision
├── Infrastructure failure
├── Utility outage
└── Other
```

An earthquake already has an excellent real-time data source in USGS: its feeds include machine-readable GeoJSON earthquake data and are updated every minute for several predefined windows. 

Weather can similarly come through the National Weather Service's alerts API, which provides active watches, warnings and advisories in machine-readable formats. 

So eventually your architecture becomes:

```text
                 EVENT INTELLIGENCE
                        │
       ┌────────────────┼────────────────┐
       ↓                ↓                ↓
    Protests        Earthquakes       Wildfires
       ↓                ↓                ↓
       └────────────────┼────────────────┘
                        ↓
              INFRASTRUCTURE GRAPH
                        ↓
                 IMPACT ENGINE
                        ↓
                 USER EXPOSURE
                        ↓
               ACTION / ALERT
```

---

# 19. The research question I'd use

If this is intended to become an actual research project rather than just an app, I'd frame it roughly as:

> **Can multi-source spatiotemporal data be used to predict infrastructure disruption and individualized exposure following rapidly evolving public events?**

And your first experimental domain would be demonstrations in the Puget Sound region.

Then you could test:

### Research Question 1
Which event characteristics are most predictive of transportation disruption?

### Research Question 2
Can historical event patterns improve short-term impact forecasts?

### Research Question 3
Can infrastructure-network structure improve prediction over geographic proximity alone?

### Research Question 4
Can personalized exposure models identify users affected by an event more accurately than region-wide alerts?

Those are much stronger research questions than simply:

> “Can AI detect protests?”

---

# 20. One important safety/design boundary

I would **not** make the core model:

> “Predict whether protesters will become violent.”

That's both much harder to validate and creates unnecessary political/social bias.

Instead:

> **Predict measurable infrastructure consequences from observable event characteristics.**

So the model can say:

> “Likelihood of additional transportation disruption is elevated.”

It should **not** infer that a particular political group or type of protester is dangerous.

Similarly, for a serious emergency, the product should surface official emergency instructions and verified evacuation information rather than inventing its own evacuation destination. Seattle's own emergency guidance emphasizes following issued evacuation routes and instructions. 

---

## The final architecture

I'd ultimately define your system as:

```text
                         ┌─────────────────────┐
                         │   DATA INGESTION    │
                         └──────────┬──────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          ↓                         ↓                         ↓
    Historical Data           Real-Time Data              News/Reports
    ACLED, etc.               WSDOT, Metro,              corroboration
                              USGS, NWS, etc.
          └─────────────────────────┬─────────────────────────┘
                                    ↓
                         ┌─────────────────────┐
                         │ EVENT RESOLUTION    │
                         │ dedupe + geocode +  │
                         │ confidence + time   │
                         └──────────┬──────────┘
                                    ↓
                         ┌─────────────────────┐
                         │ EVENT UNDERSTANDING │
                         │ ~40+ variables      │
                         └──────────┬──────────┘
                                    ↓
                         ┌─────────────────────┐
                         │ HISTORICAL ANALOGS  │
                         │ + ML FORECASTING    │
                         └──────────┬──────────┘
                                    ↓
                         ┌─────────────────────┐
                         │ INFRASTRUCTURE      │
                         │ IMPACT ENGINE        │
                         └──────────┬──────────┘
                                    ↓
                         ┌─────────────────────┐
                         │ USER EXPOSURE       │
                         │ location + routes   │
                         └──────────┬──────────┘
                                    ↓
                   ┌────────────────┴────────────────┐
                   ↓                                 ↓
             INFORMATION                       ACTION
             "What's happening?"               "What do I do?"
                   │                                 │
                   └────────────────┬────────────────┘
                                    ↓
                            PERSONALIZED ALERT
```

**The really compelling part is that the protest is no longer the product.** It's simply the first event class your system understands deeply. The product is the **real-time translation of chaotic regional events into evidence-backed infrastructure impacts for a specific person.**

And the Seattle/Puget Sound MVP is unusually feasible because you can combine historical demonstration data with genuinely live transportation, transit, emergency, earthquake and weather feeds rather than having to fabricate a real-time dataset yourself. 

## You

Do, do more. I don't want this to be hardcoded, by the way. Nothing about this should be hardcoded. Like, the flow should not be hardcoded, as in, like during the first flow, only things it looks at is these three things. Second flow only looks at these three things. It should be, like, dynamic. Everything is— every protest is different. Every protest is based around its innate characteristics, right? There could be peaceful protests, and all you have to do is just analyze this thing. It never reaches other levels. Like, that's all it is. Like, I meant, like, what I— levels don't mean levels as in levels mean reactive levels, right? So you have one analysis, and the first analysis, like every analysis runs everything, but it only shows what is picked up as important to the user in terms of its scale, right? First analysis runs, blah blah blah blah blah. But then you get an update, any sort of update. It could be police reporting an update. It could be any update, and then it runs a second analysis about based on the update, what has changed, right? And then based on the analysis and multiple analysis, right? That should be kind of, like, how it's based around for the user.

## ChatGPT

Exactly. That changes the architecture in an important way: **there should be no predefined “Level 1 → Level 2 → Level 3” workflow at all.** The system should continuously reassess the entire situation, and the UI should dynamically surface whatever matters most to that particular user at that particular moment.

## 1. The fundamental model

The system should operate like this:

```text
                    ┌──────────────────────┐
                    │      LIVE EVENT      │
                    │  "Something changed" │
                    └──────────┬───────────┘
                               ↓
                    ┌──────────────────────┐
                    │   ANALYSIS ENGINE     │
                    │                      │
                    │ Analyze EVERYTHING   │
                    │ relevant to event    │
                    └──────────┬───────────┘
                               ↓
              ┌────────────────┴────────────────┐
              ↓                                 ↓
       EVENT STATE                         USER STATE
              │                                 │
              └────────────────┬────────────────┘
                               ↓
                    ┌──────────────────────┐
                    │ IMPACT ASSESSMENT    │
                    │ + UNCERTAINTY        │
                    └──────────┬───────────┘
                               ↓
                    ┌──────────────────────┐
                    │ DYNAMIC PRIORITIZER  │
                    │                      │
                    │ What actually matters│
                    │ to THIS user now?     │
                    └──────────┬───────────┘
                               ↓
                    ┌──────────────────────┐
                    │ USER INTERFACE       │
                    └──────────────────────┘
```

There is **one analysis engine**, not three separate levels.

---

# 2. Every analysis should examine the entire situation

This is the biggest change.

You don't want:

```text
IF protest:
    analyze roads

IF protest gets worse:
    analyze police

IF evacuation:
    analyze evacuation
```

That's hardcoded and brittle.

Instead:

```text
NEW INFORMATION
       ↓
RECONSTRUCT CURRENT WORLD STATE
       ↓
ANALYZE ALL RELEVANT DIMENSIONS
       ↓
DETERMINE WHAT CHANGED
       ↓
DETERMINE WHAT MATTERS
       ↓
UPDATE USER VIEW
```

The system doesn't know ahead of time which dimensions will matter.

It **discovers that from the event**.

---

# 3. Think of the event as a constantly changing state

Instead of treating a protest as a single database row:

```text
Protest #483
```

you treat it as a **living event state**.

For example:

```text
EVENT 483

t0
Location: Pioneer Square
Estimated size: 300
Road impact: Low
Transit impact: Low
Emergency response: Normal

        ↓

t1
New police report
Location expanding north
Estimated size: 900

        ↓

t2
SDOT reports road closure

        ↓

t3
Metro reports route disruption

        ↓

t4
News reports event moved again
```

The system doesn't overwrite what happened.

It creates a **timeline of state changes**.

---

# 4. Every new piece of information becomes an update

This is where your second-analysis idea becomes really good.

Suppose the first analysis happens at 3:00.

### Analysis #1

The system receives:

- news report
- police report
- transportation data
- historical information
- weather
- infrastructure map
- user location/routes

It runs the complete analysis.

Result:

```text
CURRENT STATE

Event: Active demonstration

Infrastructure impact:
Low → Moderate

User impact:
Low

Primary affected system:
Road transportation

Confidence:
72%
```

The user sees something concise.

---

# 5. Then something changes

At 3:07:

> SDOT reports a major road closure.

You **do not run a special “road closure flow.”**

You simply say:

```text
NEW INFORMATION
↓
Update event state
↓
Run complete analysis again
↓
Compare with previous analysis
```

Now the engine might determine:

```text
BEFORE

Road impact: Moderate
Transit impact: Low
User impact: Low


AFTER

Road impact: High ↑
Transit impact: Moderate ↑
User impact: High ↑
```

The user doesn't need to see everything that the model recalculated.

They need to see:

> **Your route is now affected because a road closure intersects your normal route.**

---

# 6. This creates an “analysis history”

This is another major component I'd add.

Every analysis produces a snapshot:

```text
Analysis #001
3:00 PM

Analysis #002
3:07 PM

Analysis #003
3:14 PM

Analysis #004
3:26 PM
```

But each analysis isn't merely a prediction.

It contains:

```text
STATE
EVIDENCE
VARIABLES
IMPACT ASSESSMENTS
UNCERTAINTIES
USER IMPACT
RECOMMENDATIONS
```

Then you can ask:

> **What changed?**

And generate:

```text
SINCE 3:07 PM

↑ Road impact increased
↑ Transit impact increased
↑ Your route became affected
↓ Confidence in original route decreased

New information:
• SDOT road closure
• Metro service alert
```

That's much more powerful than a static alert system.

---

# 7. Don't hardcode the variables either

Even the 40 variables I gave you previously should **not necessarily be fixed model inputs**.

You can have a **candidate feature universe**, but the analysis engine dynamically selects which features are relevant.

For example, imagine two events.

### Event A

A peaceful demonstration remains stationary for two hours.

The system might determine:

```text
Relevant:
location
duration
road proximity
transit proximity
crowd density
traffic
user proximity

Low relevance:
weather
bridge dependency
evacuation
emergency facilities
historical escalation patterns
```

### Event B

A demonstration is rapidly moving through downtown.

Now:

```text
Relevant:
movement velocity
direction
road network
transit
crowd density
police activity
event expansion
alternative routes
user location
historical movement patterns
```

### Event C

A major earthquake occurs nearby.

The system might dynamically elevate:

```text
magnitude
depth
distance
aftershock activity
building vulnerability
bridge status
road accessibility
utility outages
hospital capacity
evacuation routes
```

Same architecture.

Completely different analysis.

---

# 8. So you need a dynamic “relevance engine”

I'd make this a core component.

```text
                    ALL AVAILABLE DATA
                           │
                           ↓
                 ┌───────────────────┐
                 │ RELEVANCE ENGINE  │
                 │                   │
                 │ What could matter │
                 │ for THIS event?   │
                 └─────────┬─────────┘
                           ↓
                 EVENT-SPECIFIC
                 FEATURE GRAPH
                           │
                           ↓
                    ANALYSIS ENGINE
```

The relevance engine should consider:

### Event characteristics

What is actually happening?

### Geographic characteristics

Where is it happening?

### Temporal characteristics

How quickly is it changing?

### Infrastructure characteristics

What infrastructure is nearby or connected?

### User characteristics

What does this particular user depend on?

### Historical characteristics

What happened in similar situations?

### Current evidence

What has actually been observed?

### Uncertainty

What don't we know?

---

# 9. The model should construct an event-specific graph

This is where I'd go beyond a traditional ML pipeline.

For every event:

```text
                  EVENT
                    │
       ┌────────────┼────────────┐
       ↓            ↓            ↓
     People       Roads       Transit
       │            │            │
       │            ├────┐      ├─── Station A
       │            │    │      └─── Route 7
       │            ↓    ↓
       │          Bridge Road B
       │
       ↓
     User
```

But the graph isn't fixed.

Nodes and relationships are added or removed based on the event.

A peaceful protest that never touches transportation might produce a relatively small relevant graph.

A rapidly expanding event could produce a huge graph.

---

# 10. Every analysis should produce two things

This is important.

## A. Absolute state

> What is the situation **right now**?

## B. Delta

> What changed **since the previous analysis**?

So:

```text
ANALYSIS #4

CURRENT STATE
─────────────
Transportation: HIGH
Transit: MODERATE
Emergency services: LOW
User exposure: HIGH

CHANGES
─────────────
Transportation ↑
Transit ↑
User exposure ↑

CAUSES
─────────────
New road closure
Event geographic expansion
Transit service alert
```

That gives you temporal intelligence.

---

# 11. The system should also detect “meaningful change”

Not every new report deserves a new user notification.

Suppose:

```text
News article #14:
"Demonstration continues downtown."
```

No meaningful state change.

You could store it.

But:

```text
Metro:
"Route 40 suspended."
```

That's meaningful.

So:

```text
NEW DATA
   ↓
Does it change the world state?
   │
 ┌─┴──────────────┐
NO               YES
 │                 │
store             reanalyze
                    ↓
             Did user impact change?
                    │
              ┌─────┴─────┐
             NO          YES
              │            │
          update log    notify
```

This prevents notification spam.

---

# 12. Your notification system becomes dynamic too

Don't hardcode:

> “If X happens, send notification.”

Instead calculate:

```text
NEW STATE
   ↓
USER EXPOSURE
   ↓
CHANGE MAGNITUDE
   ↓
CONFIDENCE
   ↓
URGENCY
   ↓
NOTIFICATION DECISION
```

For one user:

```text
Event changed significantly
+
User is 10 miles away
+
No affected route
=
No notification
```

For another:

```text
Same event
+
User's saved destination is inside impact area
+
Their route is blocked
=
Immediate notification
```

Same event.

Different user.

Different output.

---

# 13. The UI should also be generated from the analysis

This is another place where you shouldn't hardcode the three layers.

Don't have:

```text
Tab 1 = Overview
Tab 2 = Impact
Tab 3 = Action
```

Instead, the backend returns **ranked information objects**.

Something like:

```text
Analysis Output

{
    situation: [...],
    impacts: [...],
    changes: [...],
    user_exposure: [...],
    actions: [...],
    evidence: [...],
    uncertainty: [...]
}
```

Then the UI decides:

> What deserves visual prominence?

---

# 14. The user might only see one thing

If nothing important is happening:

```text
ACTIVE EVENT

Minor demonstration downtown.

No significant impact to your
saved routes or locations.

Last updated 4 minutes ago.
```

That's it.

Don't overwhelm them.

---

# 15. But another event could generate this

```text
⚠️ YOUR ROUTE CHANGED

A road closure was just reported
along your normal route.

Your route:
Home → School

Estimated delay:
+18 minutes

Alternative available:
Yes

Why this changed:
New SDOT closure report
```

The interface is completely different because the **analysis determined that the user impact is different**.

---

# 16. And a major emergency could produce something completely different

```text
⚠️ IMMEDIATE IMPACT

Your current location is within
the affected area.

Official emergency instructions
have been issued.

FOLLOW OFFICIAL INSTRUCTIONS

[View official instructions]

Source:
Emergency Management
Updated 2 minutes ago
```

Again, same system.

No separate hardcoded “Level 3 screen.”

The **state determines the presentation**.

---

# 17. I would make the backend event-driven

Your architecture could look like:

```text
DATA SOURCES
     │
     ↓
┌─────────────────┐
│ EVENT STREAM     │
└────────┬────────┘
         ↓
┌─────────────────┐
│ EVENT RESOLVER  │
│                 │
│ identity        │
│ location        │
│ time            │
│ confidence      │
└────────┬────────┘
         ↓
┌─────────────────┐
│ EVENT STATE     │
│ STORE           │
└────────┬────────┘
         ↓
┌─────────────────┐
│ ANALYSIS        │
│ ORCHESTRATOR    │
└────────┬────────┘
         ↓
┌─────────────────┐
│ DYNAMIC         │
│ RELEVANCE       │
│ ENGINE          │
└────────┬────────┘
         ↓
┌─────────────────┐
│ IMPACT MODELS   │
│                 │
│ spatial         │
│ temporal        │
│ infrastructure  │
│ historical      │
│ user-specific   │
└────────┬────────┘
         ↓
┌─────────────────┐
│ STATE DELTA     │
│ ENGINE          │
└────────┬────────┘
         ↓
┌─────────────────┐
│ USER IMPACT     │
│ ENGINE          │
└────────┬────────┘
         ↓
┌─────────────────┐
│ DYNAMIC UI /    │
│ NOTIFICATION    │
└─────────────────┘
```

---

# 18. The analysis orchestrator should be autonomous

This is where your agentic component comes in.

Instead of:

```text
Step 1 → Check protest size
Step 2 → Check roads
Step 3 → Check transit
Step 4 → Check weather
```

the orchestrator receives:

```text
EVENT UPDATE

"Police report event has moved
approximately 0.8 miles north."
```

And determines:

> What do I need to investigate to understand whether this changes the situation?

It might decide to retrieve:

```text
→ current event coordinates
→ road network
→ transit routes
→ current traffic
→ nearby critical infrastructure
→ historical movement patterns
→ user routes
```

Another update might cause an entirely different retrieval set.

**That's the non-hardcoded behavior you're describing.**

---

# 19. But don't let the LLM arbitrarily invent everything

There's an important engineering distinction.

You want:

**dynamic orchestration**

not:

**uncontrolled AI behavior**

The system should have a registry of capabilities:

```text
CAPABILITIES

get_traffic()
get_transit()
get_weather()
get_emergency_reports()
get_event_history()
query_news()
query_infrastructure()
calculate_route()
query_user_exposure()
...
```

The orchestrator chooses which capabilities are relevant.

It doesn't hardcode:

> “Protests always use traffic + transit.”

Instead:

> “Given the current state, which available information would reduce uncertainty or improve the impact assessment?”

That's much more powerful.

---

# 20. Add an uncertainty-reduction loop

This could make your architecture particularly strong.

The engine asks:

> **What do I currently not know that could materially change the result?**

Example:

```text
CURRENT ASSESSMENT
User impact: HIGH

UNCERTAINTY
We don't know whether the road closure
is still active.

VALUE OF INFORMATION
HIGH

ACTION
Query latest transportation feed.
```

Then:

```text
NEW DATA
↓
Recalculate
↓
Uncertainty decreases
↓
Final assessment
```

That's much closer to an intelligent decision-support system than a normal dashboard.

---

# 21. Multiple analyses can disagree

You also shouldn't assume the system gets one perfect answer.

For example:

```text
Analysis #1
User impact: MODERATE
Confidence: 61%

Analysis #2
User impact: HIGH
Confidence: 84%

Reason:
New official road closure confirmed.
```

The system can explain the change.

That creates an **auditable chain of reasoning**:

```text
Evidence
  ↓
Interpretation
  ↓
Assessment
  ↓
Change
  ↓
New assessment
```

---

# 22. The historical database should also be dynamic

Instead of simply storing:

```text
Protest → outcome
```

store **event trajectories**:

```text
EVENT
t0 → t1 → t2 → t3 → outcome
```

Then your ML system can learn:

> Given the state at time `t`, what types of state changes historically followed?

That gives you a genuine temporal prediction problem.

For example:

```text
Current state
      ↓
Find historically similar states
      ↓
Compare trajectories
      ↓
Estimate possible future impacts
```

And importantly:

**possible future impacts ≠ guaranteed prediction.**

The UI can say:

> “Historically similar events were followed by transportation disruption.”

rather than pretending it knows exactly what will happen.

---

# 23. This gives you a much better core data model

I'd use something roughly like:

```text
EVENT
│
├── Identity
├── Current state
├── Historical states
├── Evidence
├── Sources
├── Entities
├── Geographic relationships
├── Infrastructure relationships
├── Temporal relationships
├── Predictions
├── Uncertainty
└── Analysis history
```

And:

```text
USER
│
├── Current context
├── Saved locations
├── Routes
├── Preferences
└── Exposure relationships
```

Then:

```text
EVENT × USER
       ↓
USER-SPECIFIC IMPACT STATE
```

That's the key object your product revolves around.

---

# 24. The most important conceptual change

Your product isn't:

> **“AI that monitors protests.”**

It's:

> **“A continuously updating infrastructure-impact intelligence system that interprets evolving events in relation to individual users.”**

Protests are your **initial domain** because they provide a difficult test case:

- events evolve
- locations change
- information is incomplete
- sources disagree
- infrastructure effects propagate
- historical patterns matter
- consequences vary geographically
- consequences vary enormously between users

If you can build the architecture correctly around that, adding earthquakes or wildfires later becomes a matter of adding **new data sources, event characteristics, models and capabilities**, not rewriting the application's fundamental workflow.

### The core loop becomes:

```text
              ┌─────────────────────┐
              │     WORLD STATE     │
              └──────────┬──────────┘
                         ↓
                 NEW INFORMATION
                         ↓
              ┌─────────────────────┐
              │ REASSESS EVERYTHING │
              └──────────┬──────────┘
                         ↓
               ┌─────────────────┐
               │ WHAT CHANGED?   │
               └────────┬────────┘
                        ↓
               ┌───────────��─────┐
               │ WHAT MATTERS TO  │
               │ THIS USER?       │
               └────────┬────────┘
                        ↓
               ┌─────────────────┐
               │ WHAT SHOULD THE  │
               │ USER SEE/DO?     │
               └────────┬────────┘
                        ↓
                    USER VIEW
                        │
                        │
              ←─────────┘
              next update
```

**That loop—not the three “levels”—should be the central architecture.** The levels are just different *amounts of information surfaced by the same continuously running analysis*, depending on what the system discovers about the current situation and the individual user's exposure.

## You

And then I want this— well, what do I need? Do I need an LLM? So we were thinking of using JAB as well, which is a— if you give it, like, a predictive—that seemed like a predictive aspect. We could possibly use that. I don't know how JAB works. Can you understand? Or what do we need an LLM, what we need to go ahead. I want this to be done to the fullest extent, right? So obviously for the greater Pacific, for the America, like we need to come up with every single one of these real-time stuff that is getting blah blah blah. We would need, like, 30 or 40 indicators of, like, data that we're getting: news reports, blah blah blah. Everything should be filing it as it's come to us for any particular event, right? And then beyond it filing in, it should be able to go ahead and have analysis on previous records, blah blah blah. It should be able to do all of these things within one system. First define what inputs we should be getting and how that flow would work, and then define... I want this to work in real life, not, like, work in concept, right? So there's a lot of real, like, things that we need to keep in mind, and then think of, like, the algorithm: LLM. What do we need? Or do we need, like, a JAB model? Do we need a machine learning model? What is the... or, like, the thing is, or, like, whether we need it for certain parts. Like we could literally use an LLM and JAB in tandem. Like we can have, like, different analysis, and we have different, like— you know how, like, this, like, we have, like, sub-workflows for each individual thing, right? And they have, like, run on different possibilities, or we can have, like, really cool machine learning models for one and, like, something for the other. There's a lot. Like you can integrate all three. You can integrate, like, four or five or six across the entire workflow. I want this to be as in-depth and as defined as it can be. Each of the possible things that we're considering, define them as deeply as you can.

## ChatGPT

Yes. If you want this to work **in real life**, I would not build it as “an LLM that looks at protest data.” I would build it as a **multi-model event-intelligence system** where each component has a very specific job.

One correction first: I searched for the “JAB” you mentioned. The results I can identify as JAB are **not a predictive infrastructure/event model**: one prominent JAB is the Java Academic Benchmark, and another is “Joint Adversarial Prompting and Belief Augmentation,” which is about LLM robustness.  If you're referring to a different JAB, send me its name/link and I can slot it into this architecture precisely.

# 1. The system I would actually build

Think of the whole thing as six layers:

```text
                 REAL WORLD
                     │
                     ▼
              DATA INGESTION
                     ��
                     ▼
             EVENT RESOLUTION
                     │
                     ▼
          CONTINUOUS WORLD STATE
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       ANALYSIS   PREDICTION   HISTORY
          │          │          │
          └──────────┼──────────┘
                     ▼
             USER IMPACT ENGINE
                     │
                     ▼
             DYNAMIC PRESENTATION
```

The key is that **none of these layers should assume that an event is a protest, that roads are important, or that evacuation is necessary**.

The system discovers that.

---

# 2. First: the data plane

For the Puget Sound MVP, I would build a data-ingestion layer with roughly **40 data signals**, grouped into categories.

Not every source needs to update every second. “Real time” should mean **as fresh as the underlying source permits**.

## A. Event / demonstration signals

### 1. Special-event permits

Seattle publicly publishes special-event permits, including demonstrations, marches, rallies, parades and other events. 

Useful for:

- expected location
- expected time
- route
- planned event
- expected duration
- permitted activity
- historical recurrence

But **never treat permits as the complete protest feed**. Seattle explicitly has exceptions for spontaneous First Amendment events, so an event can exist without a permit. 

### 2. Park-use permits

Useful for planned public gatherings, including protests/rallies. 

### 3. Live police calls

Seattle's public call data represents police response activity and includes calls ranging from active emergencies to problem-solving requests. The newer dataset releases more than 95% of calls but intentionally only exposes location at the police-beat level. 

### 4. Seattle police near-real-time response map

SPD describes its Calls for Service map as displaying police responses from the previous 24 hours as close to real time as possible. 

### 5. Seattle Fire 911

Seattle Fire's Real-Time 911 page automatically refreshes every 60 seconds. 

### 6. News/event detection

For broad news coverage, you can use something such as GDELT, which provides real-time APIs and event/news data, with GDELT 2.0 event data updating every 15 minutes. 

A commercial alternative such as Event Registry provides real-time media monitoring and event grouping across a large news corpus. 

### 7. Historical demonstrations

ACLED is valuable for historical training/evaluation because its US data records dates, actors, locations, fatalities and event types and uses a large source-review process. 

But **ACLED should not be your “happening in the next 10 minutes” sensor**. Its published data workflow is weekly. 

---

# 3. Transportation signals

This should be one of your largest data families.

### 8. Highway incidents

WSDOT Traveler Information API.

### 9. Highway alerts

WSDOT.

### 10. Traffic flow

WSDOT traffic sensors.

This is particularly useful because WSDOT says its traffic-flow data covers Seattle and other Washington regions and is generally updated every **90 seconds**. 

### 11. Travel times

WSDOT.

### 12. Traffic cameras

WSDOT.

### 13. Road restrictions

WSDOT.

### 14. Bridge conditions

WSDOT.

WSDOT's API exposes highway alerts, cameras, bridge-clearance information, travel information and other transportation data. 

---

# 4. Seattle street infrastructure

### 15. Current right-of-way incidents

Seattle's SDOT ROW Impacts API is specifically designed for current traffic incidents and construction events obstructing public right-of-way. 

### 16. Street closures

Seattle publishes short-term street closures as open data. 

### 17. Street-use permits

Useful for expected future disruptions.

### 18. Road classification

You need to know whether an affected road is a tiny residential street or a major arterial.

Seattle publishes its street network/classification data. 

### 19. Construction

Useful for determining whether an alternate route is actually viable.

### 20. Planned transportation impacts

This gives you the **baseline state** before the event happens.

---

# 5. Transit signals

### 21. King County Metro trip updates

### 22. Metro vehicle positions

### 23. Metro service alerts

King County provides GTFS-RT feeds for all three. 

### 24. Sound Transit vehicle positions

### 25. Sound Transit trip updates

### 26. Sound Transit service alerts

Sound Transit provides real-time information across regional agencies and publishes GTFS-RT service-alert feeds. 

### 27. Washington State Ferries

Important in Puget Sound because a disruption isn't always a road problem.

Sound Transit's regional real-time ecosystem includes Washington State Ferries among supported agency IDs. 

---

# 6. Environmental / emergency signals

### 28. Earthquakes

USGS real-time earthquake feeds provide machine-readable feeds including GeoJSON and other formats. 

### 29. Weather warnings

NWS alerts API.

### 30. Severe weather

### 31. Wind

### 32. Flooding

### 33. Heat/cold

NWS's alerts API provides watches, warnings and advisories through CAP and JSON-based interfaces. 

### 34. Federal emergency alerts

FEMA's IPAWS archive gives you historical CAP alert data for research. It is deliberately delayed by 24 hours, so it should **not** be treated as a live emergency feed. 

---

# 7. Infrastructure context

These aren't necessarily live feeds. They're the **static world model** against which live events are interpreted.

### 35. Roads

### 36. Bridges

### 37. Transit stations

### 38. Hospitals

### 39. Schools

### 40. Fire stations

### 41. Police stations

### 42. Emergency facilities

### 43. Major public buildings

### 44. Utilities / utility-service areas

### 45. Population density

### 46. Land use

### 47. Pedestrian infrastructure

### 48. Bike infrastructure

### 49. Geographic bottlenecks

### 50. Critical infrastructure dependencies

So I'd actually target **~50 signals**, rather than arbitrarily stopping at 30–40.

---

# 8. But don't store this as one giant table

This is extremely important.

Don't do:

```text
protest_id
road_impact
transit_impact
weather
news_count
police_count
...
```

Instead, build an **event ledger**.

Every incoming observation becomes an immutable record:

```text
OBSERVATION

observation_id
timestamp_received
timestamp_source
source
source_type
event_candidate
location
geometry
observation_type
raw_payload
normalized_payload
confidence
```

Example:

```text
15:42:07
Source: SPD
Type: police CAD
Location: Beat X
Observation: increased police response
Confidence: official
```

Then:

```text
15:43:18
Source: news
Type: article
Location: downtown
Observation: demonstration moving north
Confidence: medium
```

Then:

```text
15:44:01
Source: SDOT
Type: road incident
Location: 4th Ave
Observation: closure
Confidence: official
```

**Nothing gets overwritten.**

---

# 9. Build a “world state” from those observations

The raw ledger is not the thing your models use.

You continuously construct:

```text
CURRENT EVENT STATE
```

For example:

```text
EVENT #842

Identity:
Demonstration / uncertain classification

Spatial:
Current footprint
Previous footprint
Movement direction
Expansion rate

Temporal:
Start time
Duration
Current activity

Infrastructure:
Road impacts
Transit impacts
Emergency impacts

Evidence:
Sources
Agreement
Conflicts

Prediction:
Possible next impacts

User exposure:
User A
User B
User C
...
```

This state is recomputed whenever important information arrives.

---

# 10. The event resolver is critical

You might receive:

```text
News article:
"Demonstrators gather downtown."

Police:
"Large gathering downtown."

Permit:
"March scheduled downtown."

SDOT:
"Road closure downtown."

```

Those aren't four events.

They're potentially **four observations of one event**.

So you need:

### Event Resolution

It answers:

> Are these observations referring to the same real-world event?

This can use:

- spatial proximity
- temporal proximity
- named entities
- locations
- event descriptions
- organizations
- routes
- semantic similarity
- source relationships

An LLM can help here, but you should combine it with deterministic geospatial/temporal matching.

---

# 11. Now: where the LLM actually belongs

This is where I'd be very deliberate.

## LLM = interpretation and orchestration

Not:

> “LLM, predict whether traffic will be disrupted.”

That's not what I'd trust an LLM with.

Use it for:

### A. Unstructured source extraction

Turn:

> “Police said the group moved north along 4th Avenue…”

into structured information.

```text
movement_direction = north
road = 4th Ave
event_movement = true
source_claim = ...
```

### B. Entity resolution

Determine whether:

> “downtown march”

and

> “demonstrators near Pike”

probably describe the same event.

### C. Relevance reasoning

Given the current event:

> Which available data sources are worth querying?

### D. Hypothesis generation

> What infrastructure relationships might matter?

### E. Evidence synthesis

Combine dozens of observations into a readable explanation.

### F. User explanation

Turn model output into:

> “Your normal route is affected because…”

### G. Tool orchestration

The LLM can decide:

> I need current transit + current road conditions + historical analogues.

Then call those tools.

---

# 12. What the LLM should NOT do

Don't ask:

> “GPT, what is the probability this protest causes a 20-minute traffic delay?”

Instead:

```text
Historical ML model
        ↓
Traffic prediction
```

Then the LLM explains the result.

Similarly, don't ask the LLM:

> “Is this emergency dangerous?”

Instead:

```text
Official emergency classification
+
validated models
+
geospatial exposure
+
infrastructure state
```

Then LLM:

> “Here's what those measurements mean for you.”

---

# 13. Machine learning is your predictive layer

This is where your historical data becomes useful.

I'd actually use **multiple ML models**, not one giant model.

## Model 1 — Event classification

Input:

```text
observations
```

Output:

```text
event type
```

Could distinguish:

- demonstration
- transportation incident
- emergency
- weather event
- earthquake
- infrastructure failure
- unknown

---

# 14. Model 2 — Impact prediction

Input:

```text
current event state
+
infrastructure state
+
historical data
```

Output:

```text
probability of:
road disruption
transit disruption
travel-time increase
geographic expansion
etc.
```

This is where models such as:

- Gradient Boosting
- XGBoost
- LightGBM
- Random Forest
- temporal models

could be tested.

You don't need a giant neural network initially.

---

# 15. Model 3 — Time-to-impact

This is particularly interesting.

Instead of:

> “Will transit be affected?”

ask:

> **“How long until transit is likely to be affected?”**

That becomes a temporal prediction problem.

```text
Current state
      ↓
T+5 minutes
T+15 minutes
T+30 minutes
T+60 minutes
```

with uncertainty intervals.

---

# 16. Model 4 — Spatial propagation

Predict:

> Where might the infrastructure impact spread?

This is where your infrastructure graph becomes useful.

```text
Road A
 ↓
Intersection B
 ↓
Road C
 ↓
Transit Route D
 ↓
Station E
```

You can model the event as propagating through a network rather than simply drawing a circle around it.

---

# 17. Model 5 — User exposure

This shouldn't be a generic ML prediction.

Much of it can be deterministic.

For example:

```text
Event polygon
       +
User route
       ↓
Geospatial intersection
       ↓
EXPOSED
```

Then ML can estimate:

> How much disruption is likely along that route?

That's a much better division of responsibilities.

---

# 18. Model 6 — Change detection

This one is essential.

Every analysis produces:

```text
STATE(t)
```

Then:

```text
STATE(t+1)
```

and calculate:

```text
ΔSTATE
```

The system can detect:

```text
location changed
severity changed
affected infrastructure changed
confidence changed
user exposure changed
prediction changed
```

This drives notifications.

---

# 19. Model 7 — Historical analogue retrieval

Before predicting, search the historical database:

> What previous situations resemble the current one?

For example:

```text
Current event
      ↓
Embedding / feature representation
      ↓
Historical event search
      ↓
Top similar trajectories
```

Then:

```text
Current:
300 → 700 people
moving north
rush hour
arterial road

Historical:
similar event
similar location
similar time
similar movement
```

This gives the prediction model additional context.

---

# 20. This is where I would consider a graph model

If you want to push the research substantially further:

### Graph Neural Network / temporal graph model

Represent:

```text
EVENT
 ↓
ROAD
 ↓
INTERSECTION
 ↓
TRANSIT
 ↓
STATION
 ↓
USER ROUTE
```

as a graph.

Then learn how disruptions propagate.

That could become one of the most technically interesting parts of the project.

---

# 21. And potentially a forecasting model

You could have:

```text
Current event state
+
past trajectory
+
infrastructure state
+
historical analogues
```

→ temporal forecasting model.

Depending on the amount of data you eventually accumulate, you could evaluate:

- gradient boosting
- temporal convolution
- recurrent models
- transformers
- graph-temporal models

**Don't start with the fanciest one.**

Start with a strong baseline and prove that the sophisticated model actually improves forecasting.

---

# 22. Statistical models still matter

You don't want everything to be ML.

You can use statistical methods for:

### Anomaly detection

Is traffic unusually high for this location/time?

### Change-point detection

Did the event's behavior suddenly change?

### Correlation / association

Which characteristics historically correspond to disruption?

### Survival analysis

How long until a particular infrastructure effect occurs?

### Calibration

Does “70% probability” actually happen roughly 70% of the time?

That last one is **very important** if you're going to show probabilities to users.

---

# 23. So your stack becomes

```text
                  DATA
                   │
          ┌────────┴────────┐
          │                 │
      STRUCTURED         UNSTRUCTURED
          │                 │
          │                LLM
          │                 │
          └────────┬────────┘
                   ↓
             EVENT STATE
                   │
       ┌───────────┼───────────┐
       ↓           ↓           ↓
    RULES       ML MODELS    GRAPH
       │           │           │
       └───────────┼───────────┘
                   ↓
              PREDICTION
                   │
              USER EXPOSURE
                   │
            DYNAMIC PRIORITY
                   │
                  LLM
                   │
             USER INTERFACE
```

---

# 24. What each technology does

| Technology | Job |
|---|---|
| APIs | Get real-world data |
| Streaming/event bus | Move new observations through system |
| Database | Store everything |
| Geospatial DB | Understand location/geometry |
| Search/vector DB | Find historical analogues and documents |
| LLM | Understand language + orchestrate tools + explain |
| Classical ML | Predict measurable outcomes |
| Time-series ML | Forecast changes |
| Graph ML | Model infrastructure propagation |
| Statistical models | Baselines, anomalies, calibration |
| Rules/constraints | Safety + deterministic facts |
| Routing engine | Calculate actual user routes |
| Notification engine | Decide whether a state change matters |
| UI | Surface dynamically selected information |

---

# 25. I would NOT have one “AI model”

You should have a **model portfolio**.

For example:

```text
                         ANALYSIS
                            │
       ┌────────────────────┼────────────────────┐
       ↓                    ↓                    ↓
   LLM Layer           ML Layer             Graph Layer
       │                    │                    │
Extraction             Impact prediction     Propagation
Resolution             Time prediction       dependencies
Reasoning              Anomaly detection     network effects
Tool selection         Classification
Explanation            Calibration
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ↓
                      STATE ASSESSMENT
```

---

# 26. The LLM becomes the orchestrator

Imagine this update arrives:

> “Police report that the demonstration has moved three blocks north.”

The LLM/orchestrator doesn't execute a fixed workflow.

It reasons:

```text
New information:
movement detected.

Potentially affected:
• road network
• transit
• user routes
• current traffic
• event footprint
```

Then it requests:

```text
get_current_event_location()
get_current_traffic()
get_transit_alerts()
get_affected_routes()
get_user_routes()
retrieve_similar_historical_events()
```

But maybe it determines that weather is irrelevant.

It doesn't waste computation querying everything.

---

# 27. However, you still need a “full analysis” concept

Your earlier point was:

> Every analysis runs everything.

I'd slightly modify that.

**Every analysis considers everything available, but dynamically allocates computation.**

There are three levels of computation:

### Cheap

Run constantly:

- state updates
- geospatial intersection
- source freshness
- anomaly detection
- event matching

### Medium

Run when state changes:

- ML predictions
- historical retrieval
- infrastructure propagation

### Expensive

Run when uncertainty/importance warrants it:

- LLM deep synthesis
- graph forecasting
- multiple-model ensemble
- complex scenario analysis

That makes the real system feasible.

---

# 28. You need an analysis trigger system

Possible triggers:

```text
NEW_SOURCE
SOURCE_UPDATED
NEW_EVENT
EVENT_LOCATION_CHANGED
EVENT_SIZE_CHANGED
INFRASTRUCTURE_CHANGED
TRAFFIC_ANOMALY
TRANSIT_ALERT
EMERGENCY_ALERT
USER_LOCATION_CHANGED
USER_ROUTE_CHANGED
MODEL_CONFIDENCE_CHANGED
PREDICTION_CHANGED
TIME_INTERVAL_ELAPSED
```

Any of these can trigger a new analysis.

**No event-specific workflow.**

---

# 29. Then the analysis engine produces a standardized output

Something like:

```text
AnalysisResult

event_state
    current_state
    state_changes

evidence
    supporting_sources
    conflicting_sources
    confidence

impact
    affected_infrastructure
    geographic_extent
    severity
    propagation

forecast
    predicted_changes
    time_horizon
    uncertainty

user
    exposure
    affected_routes
    affected_locations

priority
    what_changed
    why_it_matters
    urgency

presentation
    recommended_information
```

The UI then receives that.

---

# 30. This is how the UI stays non-hardcoded

Instead of:

```text
if protest:
    show protest card
```

the analysis outputs:

```text
DISPLAY OBJECTS

1. User route changed
2. Road closure
3. Transit disruption
4. Evidence
5. Historical comparison
```

The frontend renders those according to priority.

Tomorrow:

```text
1. Earthquake detected
2. Official warning
3. Bridge status
4. User exposure
```

Same frontend.

Different data.

---

# 31. The priority system

You said you want the system to analyze 30–50 things but only show the important ones.

Exactly.

I'd calculate something like:

```text
Priority =
    User Exposure
  × Impact Magnitude
  × Urgency
  × Confidence
  × Change Magnitude
```

But **don't make this a hand-tuned magic formula forever**.

Initially you can use an interpretable scoring framework.

Eventually, learn the prioritization from:

- user interactions
- alert dismissals
- alert opens
- route changes
- whether predicted impacts occurred

while keeping safety-critical thresholds deterministic.

---

# 32. Example

The system analyzes 47 variables.

It finds:

```text
Road closure          HIGH
User route overlap    HIGH
Transit disruption    MEDIUM
Crowd movement        MEDIUM
Traffic anomaly       HIGH
Weather               LOW
Historical similarity MEDIUM
Police activity       LOW
```

The user sees:

> **Your route is affected.**

Then:

> A road closure intersects your route and traffic has increased on the primary alternative.

They don't see:

> “Weather variable = 0.17.”

But they can tap:

> **Why?**

and see all 47 factors if they want.

---

# 33. Historical data becomes a separate intelligence layer

You should maintain:

```text
EVENT HISTORY
     │
     ├── raw observations
     ├── event trajectories
     ├── infrastructure outcomes
     ├── prediction outcomes
     └── user-impact outcomes
```

Then every event teaches you something.

Eventually:

```text
Prediction:
Transit disruption probability = 72%

Actual:
Transit disruption occurred.

```

Store that.

Now your evaluation system knows whether your model is actually good.

---

# 34. You absolutely need an evaluation framework

This is the part that separates a real research system from a demo.

For every prediction:

```text
PREDICTION
time
event
predicted outcome
probability
horizon
model version
features
```

Then later:

```text
GROUND TRUTH
actual outcome
actual time
actual geographic area
```

Compare them.

Metrics:

- precision
- recall
- F1
- AUROC
- Brier score
- calibration
- MAE
- RMSE
- time-to-detection
- false-alert rate
- missed-event rate

And especially:

### Forecast horizon

How early did you correctly identify the impact?

---

# 35. You need a source-confidence system

Every observation gets:

```text
source reliability
freshness
specificity
independence
corroboration
```

So:

```text
Official transportation alert
→ high source reliability

Established local news
→ medium/high

Unverified report
→ low

Old historical article
→ not evidence of current state
```

Multiple independent sources can increase confidence.

But **five articles copying the same original report aren't five independent confirmations**.

Your event-resolution layer should try to identify that.

---

# 36. Real-world limitation: “everything”

You cannot literally collect every real-time signal.

For example, Seattle's public police data deliberately limits location granularity for privacy. 

And ACLED is historical/periodically updated rather than a live event detector. 

And special-event permits don't cover every spontaneous demonstration. 

So the correct design is:

> **Multi-source evidence fusion**

not:

> “Find the one perfect protest API.”

---

# 37. Your “truth hierarchy”

I'd establish this explicitly.

### Tier 1
Official machine-readable data.

### Tier 2
Official human-readable reports.

### Tier 3
Established news organizations.

### Tier 4
Structured third-party event databases.

### Tier 5
Unverified reports.

And the model should preserve the source provenance.

---

# 38. One particularly important distinction

Separate:

### Observation

> “SDOT reports a road closure.”

### Interpretation

> “The closure appears related to the event.”

### Prediction

> “Traffic disruption may spread.”

### Recommendation

> “Your current route is affected.”

Those are four different things.

Never collapse them into one AI-generated sentence internally.

---

# 39. The final architecture I'd recommend

```text
                         ┌────────────────────────┐
                         │     REAL WORLD         │
                         └────────────┬───────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ↓                             ↓                             ↓
 Government                    Transportation                    News
 APIs                           APIs / GTFS                    / Events
        │                             │                             │
        └─────────────────────────────┼─────────────────────────────┘
                                      ↓
                         ┌────────────────────────┐
                         │    INGESTION BUS       │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │  RAW OBSERVATION STORE  │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │   EVENT RESOLUTION      │
                         │                         │
                         │ Same event?             │
                         │ New event?              │
                         │ New observation?        │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │   LIVE WORLD STATE      │
                         └────────────┬───────────┘
                                      ↓
                   ┌──────────────────┼──────────────────┐
                   ↓                  ↓                  ↓
               LLM AGENT          ML MODELS        GRAPH ENGINE
                   │                  │                  │
             extraction          forecasting        propagation
             reasoning           classification      dependencies
             orchestration       anomaly detection   infrastructure
             synthesis           calibration
                   └──────────────────┼──────────────────┘
                                      ↓
                         ┌────────────────────────┐
                         │   ANALYSIS SNAPSHOT    │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │     DELTA ENGINE        │
                         │                         │
                         │ What changed?           │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │   USER EXPOSURE         │
                         │                         │
                         │ Where are they?         │
                         │ Where are they going?   │
                         │ What do they depend on? │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │ DYNAMIC PRIORITIZATION  │
                         └────────────┬───────────┘
                                      ↓
                         ┌────────────────────────┐
                         │ DYNAMIC UI / ALERTS     │
                         └────────────────────────┘
```

## So, do you need an LLM?

**Yes—but it should not be the predictive brain.**

I'd use:

**LLM**
→ understand messy information, resolve entities, decide what information to retrieve, synthesize evidence, explain results.

**Classical ML / gradient boosting**
→ predict concrete infrastructure outcomes from structured historical data.

**Time-series models**
→ predict how impacts evolve.

**Graph algorithms / graph ML**
→ model how disruption propagates through infrastructure.

**Geospatial algorithms**
→ determine actual exposure and route intersections.

**Statistical models**
→ anomalies, baselines, uncertainty, calibration.

**Rules**
→ hard safety constraints and official instructions.

**Event/state engine**
→ maintain the continuously changing world.

**Data pipeline**
→ ingest and preserve everything.

And **JAB should not be assumed to fill the predictive role** until we identify exactly which JAB you're referring to; the JAB results I found don't match that purpose. 

The most important design principle is that **the LLM chooses and coordinates analyses; it does not get to invent the facts or replace the predictive models.** That gives you the dynamic, non-hardcoded system you want while still making it technically defensible and actually buildable.

## You

I said Jev, G-E-V, by the way.

## ChatGPT

Ah — **GEV**, not JAB. That makes much more sense in this context.

If you mean **GEV (Graph-based/Generative Event forecasting or the specific GEV model/framework you have in mind)**, I don't want to assume which implementation you mean because “GEV” can refer to several different things in ML/statistics. If you send me the **paper, GitHub repo, or exact name** of the GEV you're referring to, I can evaluate it against the architecture above and tell you exactly:

- where GEV fits,
- what inputs it needs,
- whether it should replace or complement an ML model,
- whether an LLM should feed it features,
- what outputs it should produce,
- how it fits into the continuous re-analysis loop,
- and whether it is actually appropriate for **real-time infrastructure-impact forecasting**.

The important part is: **I would not make GEV, an LLM, or any single model the entire system.** The architecture should remain:

**live data → event/state reconstruction → dynamic feature selection → specialized predictive models (potentially GEV) → impact/propagation analysis → user-specific exposure → dynamic prioritization → LLM explanation/UI.**

Send me the GEV you're referring to and I'll go much deeper on that specific model.

## You

J-E-V. J-E-V.

## ChatGPT

Yes — **Jev, J-E-V**. I understand now.

I looked up the current Jev model specifically. It is **not a forecasting model in the traditional ML sense**. Jev is a “System One” decision model: you give it a state plus **typed questions**, and it returns things like a choice, score, or yes/no probability that software can directly use. 

That actually makes it **very interesting for your architecture**.

### Where Jev fits

I would use the three technologies like this:

| Component | What it should do |
|---|---|
| **LLM** | Understand messy reports, extract information, synthesize evidence, decide what information to investigate |
| **Jev** | Make fast, bounded decisions from the already-structured event state |
| **Predictive ML** | Actually forecast measurable future outcomes |
| **Geospatial/graph algorithms** | Calculate physical infrastructure/user exposure |
| **Rules** | Handle official instructions, hard constraints, safety-critical logic |

Jev can answer questions like:

> “How urgent is this event for this user?”

> “Is the user's route likely to be materially affected?”

> “Which impact category deserves attention?”

> “Should this new observation trigger a deeper analysis?”

> “Does this new report represent a meaningful change from the previous state?”

Those are exactly the kinds of **bounded decisions** Jev is designed for. It supports Choice, Score, and probability-style (“Noul”) outputs. 

But I **wouldn't use Jev as the main forecasting model**. For something like:

> “What will traffic look like 30 minutes from now?”

you want an actual time-series/forecasting model trained on historical transportation/event data.

So the really powerful architecture is:

```text
LIVE DATA
   ↓
LLM
"Understand what just happened"
   ↓
EVENT STATE
   ↓
┌─────────────────────────────────────┐
│                                     │
│  Predictive ML   Graph/Geo   Jev    │
│      ↓              ↓        ↓      │
│  Forecast       Exposure   Decisions │
│                                     │
└──────────────────┬──────────────────┘
                   ↓
              NEW STATE
                   ↓
             CHANGE / DELTA
                   ↓
              USER IMPACT
                   ↓
                Jev
        "What matters most?"
                   ↓
              LLM
      "Explain it to the user"
```

And importantly, **Jev can sit inside the dynamic orchestration rather than creating another hardcoded workflow**.

For example, a new police report arrives.

The LLM extracts:

```text
event_location changed
movement detected
event_size increased
```

The predictive models recalculate relevant forecasts.

The geospatial system recalculates infrastructure exposure.

Then Jev can simultaneously answer bounded questions such as:

```text
Choice:
What requires attention?
→ transportation
→ transit
→ emergency services
→ no significant impact

Score:
How significant is the user impact?
→ 0–10

Noul:
Is this a meaningful change requiring a user update?
→ 0.91

Noul:
Is the user's current route materially affected?
→ 0.84
```

Those results can then drive your software.

That is actually **a much better use of Jev than trying to make Jev “predict the protest.”**

And Jev's documented design specifically emphasizes putting it **beside an LLM rather than replacing one**: the LLM handles open-ended reasoning/generation, while Jev handles smaller machine-consumable decisions. 

So for your project, I'd now explicitly make **LLM + Jev + specialized predictive ML + geospatial/graph analysis** the candidate architecture rather than choosing one AI model.

The next step I'd take is to design the **actual 40–50 input streams → normalized event schema → feature/variable universe → individual ML models → Jev decisions → LLM orchestration → continuous re-analysis loop**, down to the actual fields each component receives and produces.

## You

Give me a summary report of everything discussed. It should be no longer than 700 words.

## ChatGPT

Here’s a condensed report capturing the full system concept and architecture discussed so far.

:::writing{variant="document" id="58324" title="Dynamic Infrastructure Impact Intelligence System — Summary Report"}
## Dynamic Infrastructure Impact Intelligence System

### Project Purpose

The system is a **real-time, user-centered infrastructure impact intelligence platform**. Protests are the initial event type, but the architecture is intended to later support earthquakes, wildfires, severe weather, transportation failures, utility outages, and other rapidly evolving events.

The system should not merely answer “What is happening?” It should determine:

**What is happening → what infrastructure is affected → how conditions are changing → how this affects a specific user → what information or action matters to them now.**

### Core Principle: Nothing Is Hardcoded

There are no fixed “Level 1, Level 2, Level 3” workflows. Every event is different.

Whenever new information arrives—police activity, road closure, transit alert, news report, emergency update, traffic anomaly, etc.—the system:

1. adds the new observation,
2. reconstructs the current event state,
3. reassesses all potentially relevant dimensions,
4. compares the new analysis with previous analyses,
5. determines what materially changed,
6. evaluates the impact on each user,
7. dynamically chooses what information deserves to be shown or alerted.

A peaceful demonstration may remain informational throughout its entire lifecycle. Another event may suddenly affect roads, transit, emergency access, or a user's commute. The event itself determines the analysis—not a predefined workflow.

### Data Architecture

The system should ingest approximately **40–50+ real-time and contextual signals**, including:

- police and fire activity,
- demonstrations and special-event permits,
- local and regional news,
- road closures,
- highway incidents,
- traffic speeds and travel times,
- traffic cameras,
- transit vehicle locations,
- transit delays and service alerts,
- ferry disruptions,
- construction,
- bridges and transportation bottlenecks,
- weather and emergency alerts,
- earthquakes,
- hospitals, schools, fire stations, public buildings, and other critical infrastructure,
- population density and land use,
- historical event records,
- user location, destination, saved locations, and normal routes.

For the Puget Sound MVP, potential data sources include Seattle Police, Seattle Fire, SDOT, WSDOT, King County Metro, Sound Transit, Washington State Ferries, NWS, USGS, ACLED, and real-time news/event systems.

Every incoming item should be preserved in an **immutable observation ledger** rather than overwriting prior information.

### Event State and Continuous Analysis

Observations are resolved into evolving real-world events. The system maintains a current **Event State** containing:

- geography and movement,
- duration and rate of change,
- affected infrastructure,
- traffic/transit effects,
- evidence and source confidence,
- uncertainty,
- historical similarity,
- predicted impacts,
- user exposure.

Every meaningful update produces a new analysis snapshot. The system tracks both:

**Absolute state:** What is true now?

**Delta:** What changed since the previous analysis?

This enables alerts such as “Your route became affected 4 minutes ago” rather than repeatedly announcing the same event.

### Multi-Model AI Architecture

No single model should control the system.

**LLM:**  
Handles unstructured reports, information extraction, entity/event resolution, dynamic tool selection, evidence synthesis, orchestration, and user-friendly explanations.

**Jev (J-E-V):**  
Handles fast, bounded decisions from structured state, such as:
- whether an update is meaningful,
- which impacts deserve attention,
- urgency,
- whether user exposure materially changed,
- which information should be prioritized.

**Predictive ML:**  
Forecasts measurable outcomes such as road disruption, transit disruption, geographic expansion, travel delay, and time-to-impact.

**Temporal models:**  
Predict how an event and its impacts may evolve over 5-, 15-, 30-, or 60-minute horizons.

**Graph/geospatial models:**  
Represent infrastructure as interconnected networks and estimate how disruption propagates across roads, intersections, bridges, transit, and user routes.

**Statistical models:**  
Handle anomalies, change-point detection, baselines, uncertainty, and probability calibration.

**Deterministic rules:**  
Protect safety-critical functions, official emergency instructions, and verified constraints.

### Dynamic User Experience

The backend may analyze dozens of factors, but the interface should show only the most important ones.

For example:

- road closure intersects your commute,
- primary alternate route is congested,
- transit service affecting your destination has changed.

Users can optionally expand a “Why?” panel to see supporting evidence, sources, confidence, historical comparisons, and additional analyzed variables.

### Ultimate Architecture

**Real-time data → observation ledger → event resolution → evolving world state → LLM + Jev + predictive ML + graph/geospatial analysis → state-change detection → user exposure → dynamic prioritization → personalized interface/alerts.**

The core product is therefore not a protest tracker. It is a **continuously updating system that translates chaotic real-world events into evidence-backed, personalized infrastructure impacts and decisions.**
:::

I can also turn this into a more formal **technical architecture brief or PRD-style section** next.

## You

Give me the technical architecture brief. This should be powerful enough so that it can start running all the backend information and we can start creating the entire backend. I want to cover everything that was discussed in the plans, that was approved. Every technical detail should be very heavily analyzed and documented. Every individual aspect that we've talked about, considered, and mentioned should be in there.

## ChatGPT

I’m turning this into an implementation-grade backend architecture, not a concept memo: data contracts, services, storage, model boundaries, re-analysis loop, confidence/provenance, alerts, APIs, deployment, and an initial build order. I’m also verifying the live Puget Sound data sources and Jev’s actual role so the design is grounded in systems you can wire up now.

## ChatGPT

The key architectural decision is now clear: **event sourcing + a continuously rebuilt world state** is the backbone. LLM/Jev/ML sit on top of that state as replaceable analysis modules; none of them owns the truth, and none of them defines the workflow. That keeps the system dynamic while still making every decision auditable and testable.

## ChatGPT

This is the implementation-grade version. I’ve separated **approved architectural principles** from **reference technology choices**, so the system stays dynamic without leaving engineering teams guessing about how to actually build it.

:::writing{variant="document" id="74163" title="Technical Architecture Brief — Dynamic Infrastructure Impact Intelligence Platform"}
# Technical Architecture Brief
## Dynamic Infrastructure Impact Intelligence Platform

**Initial deployment domain:** Seattle / Greater Puget Sound  
**Initial event class:** demonstrations and protests  
**Designed expansion:** earthquakes, severe weather, wildfire, flooding, transportation incidents, utility failures, public-safety emergencies, infrastructure failures, and other rapidly evolving regional events.

---

# 1. System Objective

The platform continuously converts fragmented real-world information into an evolving model of regional conditions and then answers one primary question:

> **What has changed in the real world, what infrastructure does it affect, and what does that change mean for this specific user?**

The system is not fundamentally a protest tracker.

The durable platform is:

```text
REAL-WORLD SIGNALS
        ↓
OBSERVATIONS
        ↓
EVENT / WORLD STATE
        ↓
MULTI-MODEL ANALYSIS
        ↓
INFRASTRUCTURE IMPACT
        ↓
USER EXPOSURE
        ↓
CHANGE + PRIORITY
        ↓
PERSONALIZED INFORMATION / ALERT
```

Protests are the initial event class because they provide a difficult real-world test: incomplete data, moving geography, inconsistent reports, rapidly changing conditions, transportation effects, and substantially different consequences for different users.

---

# 2. Non-Negotiable Architectural Principles

## 2.1 No hardcoded event workflow

The backend must never contain logic equivalent to:

```text
IF protest:
    check roads
    check transit
    check police
```

or:

```text
Level 1 → Level 2 → Level 3
```

There is only one continuously operating analysis system.

Every meaningful new observation triggers reconsideration of the current state.

The “levels” discussed previously are **presentation depth**, not backend workflow stages.

An event may remain informational for its entire lifecycle.

Another event may suddenly produce:

- transportation impacts,
- transit disruptions,
- public-safety consequences,
- infrastructure failures,
- emergency instructions,
- user-route changes.

The data determines which dimensions become important.

---

## 2.2 Event sourcing is the backbone

Incoming information must never simply overwrite previous information.

Every input becomes an immutable observation.

```text
Observation 0001
Observation 0002
Observation 0003
...
```

The current event state is reconstructed from those observations.

This provides:

- auditability,
- historical replay,
- model training,
- debugging,
- confidence reconstruction,
- source provenance,
- temporal analysis,
- retrospective evaluation.

---

## 2.3 Separate fact from inference

Internally, the platform must distinguish four concepts:

```text
OBSERVATION
"SDOT reports 4th Avenue closed."

INTERPRETATION
"The closure is likely associated with Event 81."

PREDICTION
"Congestion may propagate to adjacent arterials."

USER RECOMMENDATION
"Your normal route is currently affected."
```

Never merge these concepts into one database value.

Every user-facing statement should ultimately be traceable back through:

```text
Recommendation
     ↓
Analysis
     ↓
Prediction / Interpretation
     ↓
Evidence
     ↓
Original observation/source
```

---

# 3. Reference Production Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                     EXTERNAL WORLD                          │
│                                                             │
│ WSDOT · SDOT · Police · Fire · Transit · Utilities         │
│ NWS · USGS · Emergency Alerts · News · Historical Data     │
└───────────────────────────┬─────────────────────────────────┘
                            │
                    SOURCE CONNECTORS
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                       EVENT BUS                             │
│                Kafka / Redpanda compatible                  │
└───────────────────────────┬─────────────────────────────────┘
                            │
                    NORMALIZATION
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                  OBSERVATION LEDGER                         │
│                                                             │
│ Raw payload + normalized observation + provenance           │
└───────────────────────────┬─────────────────────────────────┘
                            │
                       EVENT RESOLVER
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    WORLD STATE ENGINE                       │
│                                                             │
│ Events · Infrastructure · Geography · Time · Evidence      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                  ANALYSIS ORCHESTRATOR
                            │
       ┌────────────────────┼──────────────────────┐
       │                    │                      │
       ▼                    ▼                      ▼
     LLM                 ML MODELS               JEV
 interpretation          forecasting           bounded
 orchestration          anomaly detection      decisions
 extraction             temporal models        scoring
       │                    │                      │
       ├────────────────────┼──────────────────────┤
       │                    │                      │
       ▼                    ▼                      ▼
 Geospatial Engine     Graph Engine       Historical Retrieval
       │                    │                      │
       └────────────────────┼──────────────────────┘
                            ▼
                    ANALYSIS SNAPSHOT
                            │
                            ▼
                      DELTA ENGINE
                            │
                            ▼
                     USER EXPOSURE
                            │
                            ▼
                  PRIORITIZATION ENGINE
                            │
                            ▼
             ALERTS / API / DYNAMIC USER INTERFACE
```

---

# 4. Data Ingestion Layer

The source layer must be plugin-based.

Every connector implements the same contract:

```text
SourceAdapter

poll()
subscribe()
normalize()
health()
checkpoint()
backfill()
```

Sources can therefore be added without modifying event logic.

Regional configurations determine which adapters operate.

```text
RegionProfile:
    region_id
    geometry
    timezone
    source_adapters[]
    infrastructure_datasets[]
```

Nothing about the analysis engine needs to know whether the deployment region is Seattle, Portland, Los Angeles, or eventually nationwide.

---

# 5. Puget Sound Source Registry

The first production deployment should ingest approximately 40 core external/context signals.

| # | Signal | Initial source |
|---:|---|---|
| 1 | Official emergency notifications | AlertSeattle |
| 2 | Police responses | Seattle Police |
| 3 | Fire/EMS dispatch | Seattle Fire |
| 4 | Current road incidents | SDOT |
| 5 | Work zones | SDOT |
| 6 | Road-event closures | SDOT |
| 7 | State highway incidents | WSDOT |
| 8 | Highway alerts | WSDOT |
| 9 | Traffic-flow readings | WSDOT |
| 10 | Travel times | WSDOT |
| 11 | Highway cameras/metadata | WSDOT |
| 12 | Bridge restrictions/clearances | WSDOT |
| 13 | Ferry schedules | WSDOT/WSF |
| 14 | Ferry vessels/status | WSDOT/WSF |
| 15 | Metro trip updates | King County Metro |
| 16 | Metro vehicle positions | King County Metro |
| 17 | Metro service alerts | King County Metro |
| 18 | Static transit network | GTFS |
| 19 | Sound Transit disruptions | Sound Transit |
| 20 | Planned demonstrations/events | Seattle Special Events |
| 21 | Power outages | Seattle City Light |
| 22 | Water outages | Seattle Public Utilities |
| 23 | Weather warnings | NWS |
| 24 | Severe-weather conditions | NWS |
| 25 | Flood/weather context | NWS/regional feeds |
| 26 | Earthquakes | USGS |
| 27 | Official tsunami/emergency information | emergency authorities |
| 28 | Near-real-time news events | GDELT |
| 29 | Local news reporting | approved publishers |
| 30 | Government press releases | agency feeds |
| 31 | Historical demonstrations | ACLED |
| 32 | Road topology | regional GIS / OSM |
| 33 | Transit topology | GTFS |
| 34 | Hospitals | government/open GIS |
| 35 | Schools | government/open GIS |
| 36 | Fire/emergency facilities | government/open GIS |
| 37 | Population / demographic density | Census-derived datasets |
| 38 | Land use | municipal GIS |
| 39 | Pedestrian/bike network | municipal GIS/OSM |
| 40 | Critical geographic bottlenecks | internally derived |

WSDOT already exposes highway alerts, traffic flow, travel times, cameras, ferry information and other transportation interfaces through its Traveler Information API. 

Seattle's ROW Impacts API exposes current road incidents and work zones and uses standardized transportation-data feeds; access requires an API key. 

King County Metro publishes static GTFS plus real-time trip updates, vehicle positions and service alerts. 

Seattle Fire's public dispatch system automatically refreshes every 60 seconds.  Seattle Police publishes responses from its CAD system as close to real time as possible, but incidents appear after the response is closed; consequently this source cannot be treated as a complete live picture of ongoing police activity. 

Seattle City Light's public outage map reports current power outages and states that its displayed information updates every five minutes, with explicit limitations on completeness and accuracy.  Seattle Public Utilities also exposes planned and emergency water outages through its live outage map. 

NWS exposes watches, warnings and advisories using JSON-LD and CAP.  USGS exposes real-time earthquake data including programmatic GeoJSON feeds. 

GDELT's event/news system updates on a 15-minute cycle and can serve as one discovery signal rather than an authoritative truth source. 

ACLED is valuable for historical demonstration/event research, but its reviewed US data is produced on a weekly process and therefore belongs primarily in the historical/training layer rather than the minute-by-minute detection layer. 

---

# 6. Observation Data Contract

Every connector must output a universal observation envelope.

```json
{
  "observation_id": "uuid",
  "source_id": "wsdot.highway_alerts",
  "source_record_id": "12345",

  "event_time": "2026-10-03T19:41:22Z",
  "observed_at": "2026-10-03T19:41:22Z",
  "ingested_at": "2026-10-03T19:41:27Z",

  "source_type": "official_transportation",
  "observation_type": "road_closure",

  "geometry": {},
  "location_precision_m": 20,

  "headline": "...",
  "structured_payload": {},
  "raw_payload_uri": "s3://raw/...",

  "source_url": "...",

  "provenance": {
    "authority": "official",
    "retrieval_method": "api",
    "content_hash": "...",
    "connector_version": "..."
  },

  "quality": {
    "source_reliability": 1.0,
    "temporal_precision": 0.95,
    "spatial_precision": 0.95,
    "extraction_confidence": 1.0
  }
}
```

Three separate timestamps are mandatory:

**event_time** — when the source says the event occurred.

**observed_at** — when the source published or updated it.

**ingested_at** — when our platform received it.

This is necessary because feeds arrive late and sometimes out of order.

---

# 7. Delivery Guarantees

Use **at-least-once ingestion plus idempotent consumers**, rather than attempting fragile end-to-end exactly-once delivery.

Deduplication key:

```text
source_id
+
source_record_id
+
source_version/update timestamp
```

Fallback:

```text
SHA256(normalized_source_payload)
```

Every consumer maintains a checkpoint.

All events remain replayable.

---

# 8. Event Bus

Reference implementation:

**Kafka or Redpanda-compatible event streaming.**

Core topics:

```text
raw.observations
normalized.observations

claims.created

events.candidates
events.updated
events.merged
events.closed

state.updated

analysis.requested
analysis.completed

impact.updated
user_impact.updated

alerts.candidates
alerts.sent

models.predictions

dead_letter
```

Partition primarily by:

```text
region_id + spatial_partition
```

and, once resolved:

```text
event_id
```

This preserves ordering for individual evolving events without forcing global ordering.

---

# 9. Raw Data Preservation

Every original payload should be retained independently of normalized records.

Recommended storage:

```text
S3-compatible object storage
```

Path:

```text
/source/year/month/day/hour/observation_id
```

Store:

- original JSON,
- XML,
- GTFS protobuf,
- HTML snapshot where permitted,
- source metadata,
- retrieval headers,
- checksums.

This ensures a parser can be improved later and all historical observations reprocessed.

---

# 10. Event Resolution

Multiple observations may describe one physical event.

Example:

```text
Permit:
"March at Westlake"

News:
"Demonstrators moving downtown"

SDOT:
"Street closure near Westlake"

Police:
"Large gathering downtown"
```

The event resolver calculates whether records should attach to an existing event or create another event.

Candidate score features include:

```text
spatial overlap
temporal distance
semantic similarity
shared named locations
shared organizations/entities
movement compatibility
shared route
source cross-reference
event-type compatibility
```

Candidate generation should first use inexpensive constraints:

```text
within time window
AND
within geographic search radius
```

followed by semantic/entity analysis.

The LLM may assist ambiguous resolution, but deterministic temporal/geospatial evidence must remain available.

No merge is irreversible.

Maintain:

```text
event_aliases
event_merge_history
event_split_history
```

---

# 11. Claim Layer

Raw observations should additionally be decomposed into atomic claims.

Example source sentence:

> Demonstrators moved north on Fourth Avenue after police closed an intersection.

Possible claims:

```text
event movement = true
movement direction = north
road = Fourth Avenue
intersection closure = true
```

Claim:

```json
{
  "claim_id": "...",
  "observation_id": "...",
  "event_id": "...",
  "predicate": "event.movement_direction",
  "value": "north",
  "geometry": {},
  "valid_from": "...",
  "valid_until": null,
  "extraction_method": "llm",
  "extraction_confidence": 0.93
}
```

Claims allow disagreement.

```text
Source A → estimated crowd 300
Source B → estimated crowd 700
```

Do not force premature consensus.

---

# 12. Event State

An event is not a database row.

It is an evolving state reconstructed from observations.

```json
{
  "event_id": "...",
  "event_type_distribution": {
    "demonstration": 0.91,
    "public_gathering": 0.07,
    "unknown": 0.02
  },

  "status": "active",

  "geometry": {},
  "geometry_confidence": 0.87,

  "first_observed": "...",
  "last_observed": "...",

  "movement": {
    "moving": true,
    "direction": 12,
    "speed_estimate": 0.8
  },

  "affected_infrastructure": [],

  "evidence_summary": {},
  "feature_vector_version": "...",

  "state_version": 38
}
```

Every material update creates:

```text
EventState v37
EventState v38
```

Never overwrite the previous snapshot.

---

# 13. Dynamic Feature System

The system must have a **feature universe**, not a fixed protest feature vector.

Each feature definition contains:

```text
FeatureDefinition

name
description
data_type
dependencies
applicable_context
computation_method
refresh_policy
cost
freshness_limit
confidence_method
version
```

Examples:

```text
event_movement_velocity
road_overlap
arterial_overlap
traffic_delta
transit_route_overlap
route_redundancy
critical_facility_proximity
power_outage_overlap
weather_severity
historical_similarity
user_route_overlap
```

The orchestration layer determines what is worth computing.

---

# 14. Candidate Feature Families

The complete feature universe should initially contain at least these families:

### Event dynamics

Event type, duration, footprint, geographic expansion, movement direction, movement velocity, estimated attendance, density, simultaneous locations, organization level, rate of change.

### Transportation

Road overlap, arterial overlap, lanes affected, current closures, traffic volume, congestion anomaly, travel-time deviation, alternate-route availability, bridge/tunnel dependency, geographic bottlenecks.

### Transit

Routes affected, stops affected, vehicles affected, delay level, cancellations, station accessibility, route redundancy, expected passenger impact.

### Infrastructure

Hospitals, fire/EMS facilities, schools, government facilities, power outages, water outages, critical infrastructure proximity.

### Environmental

Weather, wind, precipitation, severe-weather alerts, visibility, earthquake conditions, flooding.

### Temporal

Time of day, weekday/weekend, rush-hour status, historical baseline, scheduled events.

### Evidence

Source count, independent corroboration, authority, freshness, contradictions, spatial precision, temporal precision.

### Historical

Similar events, similar event states, similar trajectories, historical infrastructure consequences.

### User

Current location, selected location, destination, transportation mode, saved routes, saved places, user-event distance, route-event overlap, alternate-route availability.

---

# 15. Relevance Engine

Every analysis conceptually considers the entire available feature universe.

It should **not necessarily compute every expensive feature every time**.

The relevance engine ranks potential analyses using approximately:

```text
Expected Relevance
×
Potential User Impact
×
Information Gain
×
Uncertainty Reduction
÷
Computation Cost
```

This creates dynamic computation.

Example:

```text
New observation:
"demonstration moved north"

Relevant computations:
✓ event geometry
✓ movement velocity
✓ road graph intersection
✓ transit intersections
✓ user-route intersection

Probably unnecessary:
× earthquake model
× electrical-outage propagation
```

The workflow was not hardcoded.

The relevance was inferred from the new state.

---

# 16. Full-Analysis Semantics

“Run everything” should therefore mean:

> Every analysis considers the complete available analytical capability registry and determines what must be refreshed.

Not:

> Execute every model regardless of relevance.

This distinction is necessary for real production performance.

---

# 17. Analysis Capability Registry

Every analytical capability follows a common interface:

```text
AnalysisCapability

capability_id
input_requirements
output_schema
supported_event_types
supported_geography
estimated_latency
estimated_cost
freshness_requirements
model_version
```

Examples:

```text
traffic_anomaly
transit_disruption
road_propagation
historical_similarity
time_to_impact
user_route_exposure
critical_facility_exposure
weather_interaction
source_conflict_analysis
```

Adding earthquake support means adding capabilities and data sources—not creating another application workflow.

---

# 18. LLM Architecture

The LLM is an **interpretation and orchestration component**, not the authoritative forecasting engine.

Use it for:

### Unstructured extraction

Convert news/government prose into claims.

### Semantic event resolution

Determine whether differently worded reports probably discuss the same event.

### Source interpretation

Understand nuanced updates that ordinary parsers cannot.

### Dynamic orchestration

Select which analytical capabilities are useful.

### Hypothesis generation

Identify which infrastructure relationships deserve examination.

### Evidence synthesis

Convert structured evidence into a concise explanation.

### User communication

Explain:

```text
WHAT changed
WHY it matters
WHAT evidence supports it
WHAT uncertainty remains
```

---

# 19. LLM Requirements

LLM outputs must use schema-constrained structured responses.

Never rely on free-form prose for backend control.

Example:

```json
{
  "new_claims": [],
  "possible_event_links": [],
  "recommended_capabilities": [
    {
      "capability": "road_network_exposure",
      "reason": "...",
      "priority": 0.92
    }
  ]
}
```

LLM prompts must contain provenance IDs.

Generated user explanations should receive already-validated facts rather than be asked to independently discover facts.

---

# 20. LLM Prohibitions

The LLM alone must not:

- declare an evacuation,
- invent evacuation destinations,
- override official emergency instructions,
- generate authoritative road closures,
- invent current facts,
- assign criminal intent,
- infer that a political group is inherently dangerous,
- estimate safety-critical probabilities without an underlying validated model.

If an official evacuation order exists, surface the official instruction and recommended routes. Seattle's emergency guidance specifically tells people subject to evacuation orders to follow the recommended official routes. 

---

# 21. Jev Integration

Jev belongs in the **bounded decision layer**.

Its current API is explicitly a decision model rather than a chat model and exposes typed `noul`, `choice`, and `score` outputs with probability information. 

Use Jev where the answer space is intentionally bounded.

Examples:

```text
Noul:
"Does this update materially alter the event state?"

Noul:
"Does the new state materially affect this user's route?"

Choice:
"What is currently the primary infrastructure impact?"
road
transit
utility
public_safety
none
other

Score:
"Rate the urgency of this user's infrastructure impact."
none
low
moderate
high
immediate
```

Possible additional decisions:

```text
needs_human_review
source_conflict_material
notification_worthy
primary_user_concern
information_priority
```

---

# 22. Jev Must Not Become the Forecasting Engine

Jev should not be asked:

```text
"What will traffic on I-5 be in 30 minutes?"
```

That belongs to a predictive model.

Instead:

```text
Predictive model:
P(severe route disruption within 30m) = .68

Jev:
"Given the current evidence and model outputs,
is this change important enough to surface prominently?"
```

Safety-critical notifications should additionally have deterministic policy constraints.

Jev is one analytical component, not the final authority.

---

# 23. Predictive Model Portfolio

There should not be one universal “risk model.”

Use specialized models.

## Model A — Event Classification

Input:

```text
structured observations
LLM-extracted claims
spatial/temporal context
```

Output:

```text
event-type probability distribution
```

---

## Model B — Infrastructure Impact

Separate outputs such as:

```text
P(road disruption)
P(transit disruption)
P(power disruption)
P(public facility impact)
P(route delay)
```

Start with interpretable models:

```text
logistic regression baseline
gradient boosted trees
LightGBM/XGBoost-style model
```

Only adopt larger neural models where measured performance justifies them.

---

# 24. Time-to-Impact Model

Predict:

```text
probability impact exists by T+5
probability impact exists by T+15
probability impact exists by T+30
probability impact exists by T+60
```

Survival-analysis methods should be considered here because the question is fundamentally time-to-event.

---

# 25. Traffic Prediction

Create a counterfactual baseline:

```text
Expected traffic
given:
location
weekday
time
season
weather
known construction
```

Compare current observations:

```text
Traffic anomaly =
Observed traffic - Expected traffic
```

Then estimate event-attributable disruption cautiously.

A traffic anomaly does not automatically imply the demonstration caused the anomaly.

---

# 26. Transit Models

Inputs:

```text
GTFS schedule
GTFS-RT
event geometry
road disruptions
vehicle positions
historical reliability
```

Outputs:

```text
affected routes
expected delay
stop accessibility
service cancellation probability
alternate transit options
```

---

# 27. Spatial Propagation Model

Impacts should not be modeled as simple circles.

Represent infrastructure as networks:

```text
ROAD EDGE
    ↓
INTERSECTION
    ↓
ARTERIAL
    ↓
BRIDGE
    ↓
TRANSIT ROUTE
```

A closure can therefore affect infrastructure beyond the event's physical footprint.

Initial implementation can use graph algorithms.

Later research can evaluate:

```text
graph neural networks
temporal graph neural networks
spatiotemporal transformers
```

against simpler baselines.

---

# 28. Geospatial Engine

Use:

**PostgreSQL + PostGIS** as the primary geospatial source of truth.

Recommended operations:

```text
ST_Intersects
ST_DWithin
ST_Distance
ST_Buffer
ST_Contains
ST_Union
```

Maintain spatial indexing with GiST.

Use H3 or another spatial grid for rapid regional partitioning, caching and aggregation.

---

# 29. Routing Engine

The routing subsystem should abstract the implementation:

```text
RoutingProvider

route()
alternatives()
isochrone()
apply_closures()
travel_time()
```

Possible engines:

```text
Valhalla
GraphHopper
OSRM
commercial routing provider
```

Dynamic road closures and penalties must be applied to the route graph.

A user recommendation cannot simply say:

> Take Route B.

The system must verify Route B does not intersect another current disruption.

---

# 30. Infrastructure Graph

Core node classes:

```text
road_segment
intersection
bridge
tunnel
transit_route
transit_stop
station
ferry_terminal
hospital
school
emergency_facility
utility_area
user_route_segment
```

Edge examples:

```text
CONNECTS_TO
SERVES
DEPENDS_ON
INTERSECTS
NEAR
ROUTES_THROUGH
```

A dedicated graph database is not initially required.

PostGIS + routing/network structures are sufficient for V1.

Adopt a dedicated graph store only if benchmarks show it is needed.

---

# 31. Historical Similarity Engine

Every state snapshot receives an embedding and structured feature representation.

Historical retrieval runs two searches:

```text
structured nearest-neighbor similarity
+
semantic/vector similarity
```

Result:

```text
Current state
        ↓
similar historical states
        ↓
their future trajectories
        ↓
additional evidence for forecasting
```

Use historical analogues as model evidence—not as deterministic predictions.

ACLED can provide historical demonstration context, while the platform's own observation ledger eventually becomes the more valuable dataset because it records infrastructure consequences and temporal trajectories that generic event datasets may not contain. 

---

# 32. State Delta Engine

For every analysis:

```text
S(t-1)
S(t)
```

calculate:

```text
ΔS = S(t) - S(t-1)
```

Changes include:

```text
event geometry changed
movement changed
event classification changed
road exposure changed
transit impact changed
confidence changed
forecast changed
user exposure changed
official guidance changed
```

Each delta gets:

```text
magnitude
confidence
novelty
affected users
urgency
```

---

# 33. Meaningful-Change Detection

Not every update generates an alert.

Example:

```text
Article:
"demonstration continues downtown"
```

may produce no material delta.

Whereas:

```text
SDOT:
"major arterial closed"
```

may substantially change infrastructure state.

Pipeline:

```text
NEW OBSERVATION
       ↓
UPDATE WORLD STATE
       ↓
CALCULATE DELTA
       ↓
MATERIAL CHANGE?
   ↓             ↓
  NO            YES
   ↓             ↓
 archive      recompute impacted users
                    ↓
              alert evaluation
```

The observation is never discarded even if it is not user-visible.

---

# 34. User Context

User modeling must be intentionally minimal.

```json
{
  "user_id": "...",
  "saved_locations": [],
  "route_profiles": [],
  "transport_modes": [],
  "notification_preferences": {}
}
```

Optional ephemeral context:

```text
current_location
active_destination
active_trip
```

Current location should have short retention unless the user explicitly asks to save it.

The architecture should not build persistent movement histories by default.

---

# 35. User Exposure Engine

User exposure is primarily deterministic and geospatial.

```text
Event / infrastructure impact
             +
User location / route
             ↓
GEOSPATIAL INTERSECTION
             ↓
USER EXPOSURE
```

Then predictive models estimate severity.

Example:

```text
event itself = moderate

road disruption = high

User A:
route does not intersect → low exposure

User B:
primary route intersects → high exposure
```

Same event.

Different output.

---

# 36. Personalized Priority

Backend analysis may generate 50 findings.

The user should see perhaps three.

Initial ranking model:

```text
UserPriority =
    ImpactMagnitude
  × UserExposure
  × Urgency
  × ChangeMagnitude
  × EvidenceConfidence
```

Do not collapse these dimensions permanently.

Store every component independently.

Eventually train a ranking model using outcomes and user behavior, subject to safety constraints.

---

# 37. Dynamic Presentation Contract

The backend must not instruct the frontend:

```text
show_level_2_screen
```

Instead return ranked presentation objects.

```json
{
  "event_id": "...",
  "presentation_items": [
    {
      "type": "route_disruption",
      "priority": 0.96,
      "headline": "Your usual route is affected",
      "evidence_ids": ["..."],
      "confidence": 0.92
    },
    {
      "type": "alternative_route",
      "priority": 0.88
    }
  ]
}
```

The UI renderer decides presentation.

Thus the same UI system can display protests, earthquakes or utility failures.

---

# 38. Emergency Guidance

Official emergency information receives special treatment.

AlertSeattle is Seattle's official emergency notification system and supports localized emergency notifications as well as information about severe weather, safety, utility disruptions and major traffic incidents. 

If official guidance exists:

```text
OFFICIAL GUIDANCE
        ↓
highest presentation priority
        ↓
preserve exact source meaning
```

Predictive analysis may supplement official guidance but may not contradict or replace it.

---

# 39. Evidence Confidence

Do not create a simplistic:

```text
confidence = .82
```

without preserving its components.

Evidence quality vector:

```text
source authority
source historical reliability
freshness
temporal precision
spatial precision
independent corroboration
source disagreement
extraction confidence
model confidence
```

A user-facing confidence can be calculated later, but the vector must remain stored.

---

# 40. Independent Corroboration

Five articles do not equal five sources if all cite the same original report.

Maintain:

```text
SourceDependencyGraph
```

Identify:

```text
original reporting
syndicated copies
government source
secondary reporting
```

Corroboration scores must discount dependent sources.

---

# 41. Data Truth Status

Every user-facing finding must have one state:

```text
CONFIRMED
official/current structured evidence

REPORTED
credible source reports it

INFERRED
derived from observations

PREDICTED
future model output
```

Never visually present these identically.

---

# 42. Data Stores

Reference stack:

| Store | Purpose |
|---|---|
| PostgreSQL/PostGIS | events, infrastructure, users, geometry |
| S3-compatible object storage | immutable raw payloads |
| Kafka/Redpanda | live event stream |
| ClickHouse | high-volume historical analytics |
| Redis | low-latency state/cache |
| pgvector initially | semantic/historical retrieval |
| model registry | ML models and metadata |

Avoid unnecessary database proliferation early.

A separate vector database or graph database should only be introduced after performance measurements justify it.

---

# 43. Analysis Run Contract

Every complete re-analysis receives an immutable run ID.

```json
{
  "analysis_run_id": "...",
  "event_id": "...",
  "trigger_observation_id": "...",

  "previous_state_version": 37,
  "new_state_version": 38,

  "features": {},
  "capabilities_invoked": [],
  "models_invoked": [],
  "jev_decisions": {},
  "llm_operations": [],

  "forecasts": [],
  "impacts": [],
  "deltas": [],

  "started_at": "...",
  "completed_at": "...",

  "software_versions": {}
}
```

This must make every production decision replayable.

---

# 44. Model Output Contract

Every ML prediction stores:

```text
model_id
model_version
prediction_time
forecast_horizon
features_version
input_state_version
output
probability
uncertainty
calibration_version
```

Never store only the displayed value.

---

# 45. Model Evaluation

Every forecast must eventually connect to ground truth.

Example:

```text
Prediction at 15:00:
P(transit disruption within 30m) = .72

Ground truth:
Transit disruption began 15:24
```

Store both.

Primary model metrics:

```text
precision
recall
F1
Brier score
calibration error
MAE
RMSE
false-alert rate
missed-impact rate
time-to-correct-detection
forecast lead time
```

Probability calibration is mandatory before exposing probabilities prominently to users.

---

# 46. Model Training Architecture

Historical training dataset must be produced from the same event ledger.

```text
OBSERVATION LEDGER
        ↓
historical state reconstruction
        ↓
point-in-time features
        ↓
known future outcomes
        ↓
training examples
```

This prevents future information leaking into historical feature vectors.

Point-in-time correctness is essential.

---

# 47. Model Registry

Use a formal registry such as MLflow-compatible infrastructure.

Store:

```text
training data range
feature schema
hyperparameters
metrics
calibration model
code commit
artifact checksum
deployment date
```

Production supports:

```text
champion model
challenger model
shadow model
```

New models should initially operate in shadow mode.

---

# 48. Continuous Learning

Do **not** allow uncontrolled models to automatically retrain and deploy themselves.

Process:

```text
new outcomes
    ↓
training dataset update
    ↓
candidate model
    ↓
offline evaluation
    ↓
historical replay
    ↓
shadow deployment
    ↓
comparison
    ↓
controlled promotion
```

---

# 49. Historical Replay

Replay is one of the most important system capabilities.

Given a previous incident:

```text
replay(event_id, timestamp)
```

the backend should reconstruct exactly what the system would have known at that moment.

This allows testing:

> Would the new model have identified the impact earlier?

without using future information.

---

# 50. APIs

Primary consumer API:

```text
GET /v1/events
GET /v1/events/{event_id}

GET /v1/events/{event_id}/timeline
GET /v1/events/{event_id}/evidence
GET /v1/events/{event_id}/analysis
GET /v1/events/{event_id}/forecast

GET /v1/user/impacts
GET /v1/user/routes/{route_id}/impact

GET /v1/regions/{region_id}/state
```

Real-time:

```text
WebSocket or Server-Sent Events

/v1/stream
```

Internal:

```text
POST /internal/observations
POST /internal/analysis/request

POST /internal/replay
POST /internal/models/evaluate
```

---

# 51. Event Query

Example:

```json
GET /v1/events/{id}
```

returns:

```json
{
  "event": {},
  "current_state": {},
  "top_impacts": [],
  "recent_changes": [],
  "forecast": {},
  "evidence_summary": {},
  "last_updated": "..."
}
```

Do not return raw model internals to normal clients.

---

# 52. Notification Architecture

Notifications must be state-change driven.

```text
Analysis
   ↓
User impact
   ↓
Delta
   ↓
Notification candidate
   ↓
Policy checks
   ↓
Deduplication
   ↓
Rate limiting
   ↓
delivery
```

Notification key:

```text
user_id
event_id
impact_type
state_version
```

Suppress repeated equivalent alerts.

---

# 53. Notification Conditions

Consider:

```text
new information value
change magnitude
user exposure
urgency
confidence
time since last alert
previous alert state
user preferences
official emergency status
```

A high-impact official emergency warning may bypass ordinary suppression policies.

---

# 54. “What Changed?” Must Be First-Class

Store explicit changes.

Example:

```json
{
  "change": "user_route_became_impacted",
  "before": false,
  "after": true,

  "causes": [
    "road_closure_581",
    "event_geometry_change_91"
  ]
}
```

This enables the system to tell the user:

> Your route is now affected because a new closure was reported.

rather than re-explaining the entire event.

---

# 55. Source Failure Handling

Every connector exposes:

```text
last_success
last_attempt
latency
error_rate
records_received
freshness
```

A source health state:

```text
healthy
delayed
degraded
offline
```

If a major input is unavailable, analysis confidence must reflect it.

Never silently treat “no data” as “nothing happening.”

---

# 56. Data Freshness

Each source has an expected freshness profile.

Example internal configuration:

```yaml
source: sfd.realtime911
expected_interval: 60s
stale_after: 180s

source: historical.acled
expected_interval: weekly
usage: historical_only
```

Freshness expectations belong in source configuration, not application logic.

---

# 57. Performance Targets

Initial production service objectives:

```text
Source ingestion:
<10 seconds after connector receives update

Normalization:
p95 <2 seconds

State update:
p95 <3 seconds

Standard analysis:
p95 <10 seconds

High-priority user-impact recomputation:
p95 <10 seconds

Notification dispatch:
target <30 seconds after material state update
```

Actual real-world detection latency remains limited by how quickly external sources publish information.

The platform cannot identify an event that no accessible source has yet observed.

---

# 58. Cost-Aware Analysis

Capabilities should be divided into tiers.

### Continuous/cheap

```text
deduplication
state reconstruction
geospatial intersections
freshness
anomalies
simple features
```

### Triggered

```text
historical retrieval
ML forecasting
routing recalculation
graph propagation
```

### Expensive

```text
large LLM synthesis
complex scenario simulation
large graph models
multi-model ensemble
```

This keeps continuous operation economically feasible.

---

# 59. Privacy Architecture

The user-centered system should collect as little user data as possible.

Store:

```text
saved place → when explicitly saved
saved commute → when explicitly saved
notification preference → necessary
```

Prefer:

```text
ephemeral current location
```

with automatic expiration.

Encrypt user location and route data at rest.

Separate identity data from mobility data.

Create explicit deletion paths.

Audit every privileged access to precise user-location information.

---

# 60. Protest-Specific Privacy Boundary

The system should analyze **events and infrastructure consequences**, not construct surveillance profiles of protesters.

Do not make:

```text
political ideology
race
religion
individual protest participation
```

predictors of dangerousness or infrastructure disruption.

The useful variables are observable conditions:

```text
location
movement
infrastructure interaction
closures
event size
traffic
transit conditions
time
historical infrastructure outcomes
```

This also makes the model easier to validate scientifically.

---

# 61. Security

Mandatory:

```text
TLS everywhere
service-to-service authentication
secret manager
API key rotation
role-based access control
database encryption
audit logging
rate limiting
WAF/API protection
dependency scanning
container scanning
```

Raw government/API credentials never reach clients.

---

# 62. Observability

Use:

```text
OpenTelemetry
metrics
distributed tracing
structured logs
```

Every request should propagate:

```text
trace_id
event_id
analysis_run_id
state_version
```

This allows an engineer to trace:

```text
source update
→ normalized observation
→ event state
→ prediction
→ user impact
→ notification
```

---

# 63. Recommended Backend Services

Deploy logical services approximately as:

```text
source-manager
connector-wsdot
connector-sdot
connector-transit
connector-emergency
connector-news
connector-weather

normalization-service
claim-extraction-service
event-resolution-service

world-state-service
feature-service
geospatial-service
routing-service
historical-retrieval-service

analysis-orchestrator
llm-service
jev-service
prediction-service
graph-analysis-service

delta-service
user-impact-service
priority-service
notification-service

public-api
websocket-gateway

evaluation-service
replay-service
```

Do not automatically turn every logical service into its own Kubernetes deployment on day one.

Start as several well-defined modules and split only where independent scaling/failure isolation requires it.

---

# 64. Recommended Language Split

Reference choice:

```text
Python
ML
LLM
data science
feature computation
geospatial analysis

Go / Rust / Java / TypeScript
high-throughput ingestion/API services
```

A strong practical starting point is:

```text
Python + FastAPI
for analytical services

TypeScript/Node or Go
for API/event infrastructure
```

The most important requirement is not language—it is stable service contracts.

---

# 65. Schema Management

Event-stream contracts must use versioned schemas.

Recommended:

```text
Protobuf or Avro
+
Schema Registry
```

Never change semantics silently.

Example:

```text
Observation.v1
Observation.v2
```

Old events must remain decodable.

---

# 66. Infrastructure Deployment

Cloud-neutral architecture:

```text
Kubernetes
Kafka-compatible streaming
managed PostgreSQL/PostGIS
object storage
Redis
ClickHouse
model-serving workers
```

Separate environments:

```text
development
staging
production
research/sandbox
```

Research models must not have direct production deployment permissions.

---

# 67. Regional Scaling

Partition infrastructure into regional cells:

```text
Puget Sound Cell
Portland Cell
Bay Area Cell
...
```

Each cell runs:

```text
source adapters
state processors
regional infrastructure graph
regional event state
```

National services provide:

```text
user identity
model registry
cross-region events
historical storage
global news ingestion
```

This prevents every Seattle event from requiring nationwide computation.

---

# 68. Initial Backend Build Order

### Foundation

Build first:

```text
event schemas
observation ledger
raw storage
event bus
PostGIS
source registry
```

### Live Puget Sound feeds

Implement:

```text
WSDOT
SDOT
Metro/GTFS
Seattle Fire
NWS
USGS
utilities
AlertSeattle
```

### Event system

Implement:

```text
event resolver
claim model
state snapshots
state replay
delta engine
```

### Infrastructure intelligence

Implement:

```text
road graph
transit graph
geospatial exposure
routing
traffic baseline
```

### User layer

Implement:

```text
saved locations
routes
user exposure
priority engine
notifications
```

### AI layer

Then introduce:

```text
LLM extraction
LLM orchestration
Jev decisions
historical retrieval
ML forecasting
```

### Advanced intelligence

Finally:

```text
time-to-impact
graph propagation
model ensembles
spatiotemporal ML
continuous evaluation
```

The data/state architecture should exist **before** sophisticated AI models are trusted.

---

# 69. Minimum Production MVP

The first system capable of genuinely operating in Seattle does not need every advanced ML model.

It requires:

```text
✓ immutable real-time ingestion
✓ WSDOT
✓ SDOT
✓ transit GTFS/GTFS-RT
✓ emergency/weather feeds
✓ event resolution
✓ PostGIS world state
✓ road/transit infrastructure graph
✓ source provenance
✓ user route exposure
✓ state deltas
✓ confidence representation
✓ dynamic presentation
✓ notification engine
✓ historical event storage
```

Then add the predictive intelligence progressively.

---

# 70. Research/Advanced Version

The fullest version becomes:

```text
50+ live/context signals

dynamic capability selection

historical event trajectories

LLM interpretation/orchestration

Jev bounded decisions

gradient-boosted impact models

survival/time-to-impact models

traffic forecasting

transit forecasting

graph propagation

spatiotemporal learning

historical analogue retrieval

calibrated uncertainty

individual user exposure

continuous event-state reconstruction

continuous retrospective evaluation
```

---

# 71. Final Runtime Loop

This is the central algorithm.

```text
while system_is_running:

    observation = receive_new_information()

    persist_raw(observation)

    normalized = normalize(observation)

    deduplicate(normalized)

    claims = extract_claims(normalized)

    event = resolve_event(normalized, claims)

    previous_state = load_current_state(event)

    new_state = rebuild_state(
        event,
        all_relevant_observations
    )

    delta = compare(
        previous_state,
        new_state
    )

    capabilities = relevance_engine.select(
        state=new_state,
        delta=delta,
        capability_registry=ALL_CAPABILITIES
    )

    results = run(
        capabilities,
        state=new_state
    )

    enriched_state = fuse(
        evidence=new_state,
        model_results=results
    )

    forecasts = forecasting_models(
        enriched_state
    )

    users = identify_potentially_affected_users(
        enriched_state
    )

    for user in users:

        exposure = calculate_user_exposure(
            user,
            enriched_state,
            forecasts
        )

        user_delta = compare_previous_user_state(
            user,
            exposure
        )

        priority = prioritize(
            exposure,
            user_delta,
            confidence,
            urgency
        )

        presentation = select_user_information(
            priority
        )

        if notification_policy_allows(
            user,
            presentation
        ):
            notify(user)

    persist_analysis_run()
```

There is no:

```text
protest_flow()
earthquake_flow()
level_three_flow()
```

Only:

```text
observation
→ state
→ relevance
→ analysis
→ change
→ user impact
```

---

# 72. Final Architectural Definition

The backend should ultimately be understood as five cooperating systems:

**1. A sensor network**  
Continuously acquires structured and unstructured information about the physical world.

**2. A world-state engine**  
Maintains an auditable, time-aware representation of what is currently believed to be happening.

**3. An intelligence engine**  
Uses deterministic algorithms, LLMs, Jev, statistical analysis, ML forecasting, historical retrieval and graph models according to their respective strengths.

**4. A personalization engine**  
Maps regional infrastructure consequences onto the locations, routes and needs of an individual user.

**5. A temporal change engine**  
Continuously asks:

> What changed since the last analysis, how certain are we, and does this change matter enough that the user should know?

That is the architecture that preserves the central requirement established throughout this design:

> **The system never assumes ahead of time what matters. It continuously reconstructs reality, evaluates every available analytical capability, identifies the variables that matter for the current event and current user, and surfaces only the most consequential evidence-backed changes.**
:::

The most useful next engineering artifact would be the **actual backend specification**: repository structure, PostgreSQL/PostGIS tables, Kafka topic schemas, Protobuf contracts, service APIs, source-adapter interfaces, and the exact V1 implementation tickets. That would take this architecture from “system design” to something a backend team can begin coding directly.

---

If you want, I can:

- Continue detailed architecture description
- Expand on Model B – Infrastructure Impact
- Describe backend deployment and monitoring plans