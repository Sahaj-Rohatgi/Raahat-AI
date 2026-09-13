# Raahat — Complete Beginner's Guide (for your presentation)

## 1. What problem does this solve, in one sentence?

Raahat is a system that keeps an eye on the mental/emotional wellbeing of crime victims and witnesses (people going through investigations, trials, or rehabilitation), and quietly alerts a real human counsellor **before** the person reaches a crisis — instead of only finding out after something bad has already happened.

Think of it like a "check engine light" for a person's psychological state, except the "engine" is a person going through a traumatic legal process, and the "mechanic" is a counsellor sitting on a dashboard somewhere.

## 2. The three people who use it (this is the easiest way to explain the whole project)

There are exactly three types of users, and the entire app is built around them:

1. **The Victim/Witness** — the person actually going through the ordeal. They log in, do short daily check-ins ("how's your mood, sleep, do you feel safe?"), and can chat with a support bot any time. There's also a big red "I need help right now" button.
2. **The Counsellor** — a human staff member who watches a dashboard of all the cases assigned to them, gets alerted the moment someone's distress score spikes, and can add notes, recommend interventions, or override the AI's judgment if they disagree with it.
3. **The Admin** — sees the big picture: all cases across districts/states, resolves alerts, manages the system.

Everything else in the project (the database, the AI model, the chatbot, the phone system) exists purely to support these three experiences.

## 3. The big picture — how the pieces talk to each other

```
                    ┌───────────────────────────┐
                    │   The Website (Frontend)   │
                    │  What the victim/counsellor/│
                    │  admin actually see & click │
                    └─────────────┬─────────────┘
                                  │  (asks questions over the internet:
                                  │   "log this person in", "here's a
                                  │   chat message", "give me the dashboard")
                                  ▼
                    ┌───────────────────────────┐
                    │     The Backend (Brain)     │
                    │  Receives requests, decides  │
                    │  what to do, enforces rules  │
                    │  (a victim can never see     │
                    │   someone else's case, etc.) │
                    └───┬───────────┬───────────┬─┘
                        │           │           │
                        ▼           ▼           ▼
                 ┌───────────┐ ┌─────────┐ ┌───────────────┐
                 │  Database │ │ AI Model │ │ Chatbot Safety │
                 │ (memory — │ │ (scores  │ │ Net (decides if │
                 │  stores    │ │ distress │ │ a message is an │
                 │  everyone's│ │ 0–100)   │ │ emergency)      │
                 │  cases)    │ │          │ │                 │
                 └───────────┘ └─────────┘ └───────────────┘

   Extra entry point (optional): someone can also just PHONE IN.
   A phone call goes through Twilio (a calling service) or a local
   softphone system (FreeSWITCH), and both of those funnel straight
   into the exact same "Backend Brain" above — same rules, same AI,
   same database. It's just another door into the same house.
```

**In plain words:** the website is the "face" of the app. Every single thing you click sends a message to the backend ("the brain"), which is a Python program running on a server. The brain checks the database (to remember who's who and what happened before), asks the AI model for a distress score, checks the chatbot's built-in "danger detector," and then sends back an answer that the website displays.

## 4. How the backend actually works, step by step

The backend is written in Python using a framework called **FastAPI**. Here's what that means in plain terms: FastAPI is a way of setting up a list of "doors" (called API endpoints) that the website can knock on. Each door does one specific job. For example:

- `POST /api/auth/login` — the "let me in" door. You send an email + password, it gives you back a ticket (a token) proving who you are.
- `POST /api/chat` — the "I want to talk" door. You send a message, it runs the message through the AI + safety checks and sends back a reply.
- `POST /api/checkins` — the "here's my daily check-in" door.
- `POST /api/cases/{id}/sos` — the panic button door. Skips all the AI thinking and immediately marks the case as Critical.
- `GET /api/dashboard` — the "show me everything" door, only usable by counsellors/admins.

There are about 25 of these doors in total, each one guarded — the backend checks your login ticket on almost every door and refuses you if you're not allowed through (e.g. a victim trying to peek at someone else's case gets a flat "no," server-side, not just hidden by the website).

**The database** is where everything is permanently remembered — like a very organized filing cabinet with folders (tables) for: cases, chat messages, alerts, notes, check-in answers, follow-up tasks, and a log of every action staff take (for accountability). It uses MySQL, a standard, widely-used database system.

**Logging in** works with real password protection — passwords aren't stored as plain readable text, they're scrambled with a one-way scrambling method (called scrypt) so even someone looking directly at the database can't read anyone's actual password.

## 5. How the "AI" actually decides someone is distressed — this is the heart of the project

This is the part judges will probe hardest, so understand it well. There are actually **two separate systems** working together, and it's important you can explain the difference:

### System A — The learned AI model (a "soft" score)

This part was trained on data (in this case, realistic *made-up/synthetic* example cases, since real victim data can't ethically be used for a hackathon prototype). It looks at:
- Your check-in answers (mood, sleep, do you feel safe, anxiety, how much pressure is the case putting on you, how many check-ins you've missed)
- Whatever you typed in the chat

...and turns that into a **distress score from 0 to 100**, which then maps to a band: Stable → Low → Moderate → High → Critical.

Under the hood it's a combination of two well-known, non-exotic machine learning techniques (nothing mysterious or "black box magic"):
- A **text classifier** (TF-IDF + Logistic Regression) that reads the free-text message and guesses which risk band it sounds like it belongs to — trained on English, Hindi, and "Hinglish" (romanized Hindi) example sentences, since real users will type in all three.
- A **Random Forest** (basically 140 small decision trees voting together) that takes all the check-in numbers plus the text classifier's opinion and produces the final 0–100 score, plus a probability of "this needs escalating," plus suggested support services (like Witness Protection, Financial Assistance, Counselling).

It can also explain itself a little — it shows which factors (e.g. "poor sleep," "low safety rating") contributed most to the score, and how confident it is.

### System B — The deterministic safety net (a "hard" rule, no AI involved)

This is arguably the smartest design decision in the whole project, so make sure you highlight it: **for genuinely dangerous messages, the AI's opinion is completely overridden by a simple, predictable rule-based checker.**

If someone types something matching patterns like "I want to die," "he is hitting me," "there's a weapon," "I can't breathe" (checked in English, Hindi, and Hinglish), the system immediately:
1. Marks the case as **Critical**, no matter what the AI model said,
2. Sends a fixed, pre-written safe response (never an AI-generated one, so there's zero risk of a language model saying something harmful in a real emergency),
3. Instantly alerts every counsellor/admin watching the dashboard (via a live connection, so they see it appear in real time without refreshing the page).

Why does this matter for your presentation? Because it shows the team understood a critical safety principle: **you never let a probabilistic, occasionally-wrong AI model be the only thing standing between a person in crisis and help.** The unpredictable "smart" part is only ever used to *route the boring, non-urgent conversation* — an actual life-threatening statement is caught by simple, auditable, 100%-predictable pattern matching.

There's also a nice detail: it understands **negation** — "I am NOT suicidal, just stressed" does not trigger an alert, but "I am not suicidal and I want to die" still does, because it looks at each part of the sentence separately.

### The optional "nice-to-have" chat brain

For everyday, non-urgent chat (not any of the dangerous stuff above), the assistant can optionally use Google's Gemini AI model to write a more natural, comforting reply — but it's wrapped in guardrails (it's told never to diagnose, never to promise confidentiality, never to claim it already contacted someone) and its replies are double-checked before showing them. If no AI key is configured, it just falls back to sensible pre-written responses. So the "smart chatbot" part is a nice extra, not something the safety of the app depends on.

## 6. What happens when someone phones in instead of using the website

This project also supports a phone call as an alternative front door, for people who don't have internet access or prefer to speak instead of type. Two ways this can work:

- **A local simulated phone line (FreeSWITCH)** — free, works entirely offline, good for a live demo: dial an extension, press 1 for a check-in (you answer 5 questions via keypad) or 9 for an immediate SOS.
- **A real, dialable phone number (Twilio)** — costs money, needs the internet exposed (via a tool called ngrok during development), lets someone actually call a real number and go through the same menu.

Either way, once your answers reach the server, they go through **the exact same brain, AI, and database as the website** — there's no separate/duller logic for phone users, which is a strong design point to mention.

## 7. What the website actually looks like (for each of the 3 roles)

- **Victim:** Home (mood log + trend chart + emergency button), Check-In (guided questionnaire), My Support Plan, Medications, Appointments, Resources, Settings — plus a floating chat bubble everywhere, which can also record voice notes (auto-transcribed).
- **Counsellor:** Dashboard, Priority Cases (sorted by risk), Handoff Queue (unresolved alerts), All Cases, Messages, Interventions, Chat Quality, Reports. Clicking into a case shows a distress trend graph, an "AI explainability" panel (why this score?), the ability to override the AI's judgment (logged for accountability), and case notes.
- **Admin:** Dashboard, People & Cases (anonymized — no names, just case IDs, to protect privacy), Alerts, Districts (a drill-down: national → state → district), Reports, Data & Integrations.

The whole website is a single file built with React (a popular way to build interactive websites) and works even with no internet connection at the demo venue, because all its libraries are bundled locally instead of loaded from the internet.

## 8. Suggested way to explain the workflow live, in front of judges

A simple demo story works best:

1. Log in as a **victim**. Do a check-in with bad answers (poor sleep, feeling unsafe). Show the score go up.
2. Send a chat message like "I want to end it all." Show the chat immediately flags it as an emergency and shows a safety message — explain this is the deterministic safety net, not the AI guessing.
3. Switch (using the built-in demo switcher) to the **counsellor** view. Show the alert appearing live on the dashboard, open the case, show the AI's explanation of the score, and show adding a note or overriding the score.
4. Switch to **admin**, show the district drill-down and the anonymized case list.
5. (Optional, if you have time/setup) Show or describe the phone call flow as an accessibility feature for people without internet.

## 9. Pros

- **Clear separation between "AI opinion" and "hard safety rule."** The most dangerous messages are caught by simple, predictable rules — not left to an AI's judgment — which is exactly the right instinct for a safety-critical system.
- **Multilingual from the ground up**, not bolted on — English, Hindi, and Hinglish are all handled in both the AI training data and the safety-net rules, including tricky cases like understanding negation ("I am NOT suicidal").
- **Real, working access control** — a victim genuinely cannot see another person's case, even by tampering with a request (verified by the project's own tests) — this isn't just something hidden in the menus.
- **Works with zero internet dependency for the core safety features** — the whole frontend runs offline, and the emergency detection needs no external AI service at all; the "fancy AI chat" (Gemini) is a nice-to-have layer that gracefully turns off if unavailable rather than breaking anything.
- **Explainable AI, not a black box** — the dashboard shows *why* a score is what it is (e.g. "poor sleep, low safety rating"), and counsellors can override it, with that override logged.
- **Multiple ways in** — website or phone call, both feeding the same backend — useful for people who don't have easy internet/smartphone access.
- **The team clearly tested for the hard, boring, unglamorous stuff** that actually matters in a real deployment: two people creating a case at the exact same second don't collide, notes survive a page reload (proving they're really saved, not just shown on screen), and login genuinely rejects wrong passwords.
- **Passwords are properly hashed** (scrypt), not stored as readable text.

## 10. Cons / limitations (know these — judges will probe here, and it's much better if you raise them yourself first)

- **All the training data is synthetic (made-up).** There is no real victim data behind the AI model, for very good ethical/privacy reasons — but it does mean the model's accuracy on real-world messages is unproven.
- **The district/state/national statistics on the Admin dashboard are hardcoded demo numbers**, not computed from real data — be upfront that this part is just a UI mockup of what the feature would look like at scale.
- **Rate-limiting and login-attempt tracking reset if the server restarts**, and won't work correctly if you ever ran multiple copies of the backend at once — a known "would need Redis in production" gap.
- **Phone calls aren't matched to the right case automatically** — in the current demo, every phone call is attributed to one fixed test case; a real deployment would need to identify the caller by their phone number.
- **No sign-up flow, password reset, or multi-factor authentication** — only a few fixed demo logins exist right now; that's fine for a prototype but would need to be built out for real use.
- **The Hindi/Hinglish safety detection covers common phrasing, not every dialect or way people might express distress** — it's a strong starting point, not exhaustive coverage of India's linguistic diversity.
- **The optional AI chat (Gemini) requires an internet connection and an API key** — without it, the chatbot still works (falls back to fixed replies), but loses its more natural, personalized conversational quality.
- **The entire frontend is one large file compiled in the browser** rather than using a modern build pipeline — totally fine for a hackathon prototype, but the team themselves note it would need restructuring to scale further.
- **This is explicitly a prototype, not a certified clinical or emergency-dispatch tool** — it's a support/monitoring aid to route people to human help faster, not a replacement for real crisis services, and should be framed that way to judges.

## 11. One-line answers to likely judge questions

- *"Is this real AI or just if-else statements?"* → Both, on purpose: real machine learning (Random Forest + text classification) drives the day-to-day distress scoring, but a simple, transparent rule-based layer is what catches actual emergencies — because you never want a probabilistic model to be the only thing standing between someone in crisis and help.
- *"What happens if the AI is wrong?"* → A counsellor can see the AI's reasoning and manually override the score, and every override is logged.
- *"What if someone doesn't have internet?"* → They can phone in instead, through a normal phone call, and it's answered by the same system.
- *"Is this using real data?"* → No — it's trained on synthetic (carefully constructed, realistic but made-up) example data, since real victim data raises serious ethical and legal concerns that are out of scope for a hackathon prototype.
- *"Can a victim see other people's information?"* → No, this is enforced on the server itself, not just hidden in the interface, and it's covered by automated tests.
