# SIH Finals Voiceover Script - Raahat

## Main Script (about 3 minutes)

Namaste judges.

Our project is **Raahat**, a distress monitoring and early support platform for crime victims and witnesses.

During an investigation, trial, or rehabilitation process, victims and witnesses often carry emotional pressure silently. In many cases, counsellors or support officers only discover the problem after the person has already reached a crisis point. Raahat is built to change that. It helps identify psychological distress early and routes the person to human support before the situation escalates.

The platform has three role-based experiences.

First, the victim or witness gets a calm and private space where they can complete short wellbeing check-ins, log mood, sleep, safety, anxiety, and case pressure, chat with a support assistant, and use a one-tap emergency button whenever they need immediate help.

Second, the counsellor gets a live caseload dashboard. Cases are sorted by risk, so the counsellor can immediately focus on people who need attention most. For each case, they can see the distress trend, alerts, notes, interventions, and the reasons behind the AI score. Importantly, the counsellor can override the AI assessment with a logged reason, because the system is designed to support human judgement, not replace it.

Third, the admin view gives district, state, and national-level visibility. It shows aggregate risk distribution, open alerts, and anonymised vulnerable-case data so authorities can understand where support is most urgently needed while still protecting privacy.

The heart of Raahat is its two-layer risk detection system.

The first layer is machine learning. The system takes structured check-in answers and chat text, then creates a distress score from zero to one hundred. It uses explainable models like TF-IDF, logistic regression, and random forests, so the dashboard can show not only the score, but also why the score was given, such as poor sleep, low safety rating, high anxiety, or missed check-ins.

The second layer is a deterministic safety net. This is one of the most important parts of the project. For urgent situations like self-harm, violence, abuse, medical emergencies, threats, or child safety concerns, we do not depend only on AI prediction. The message is checked using fixed multilingual safety rules in English, Hindi, and Hinglish. If a danger pattern is found, the system immediately marks the case as Critical, sends a safe predefined response, and alerts counsellors and administrators in real time.

This separation matters because in a safety-critical system, a probabilistic AI model should never be the only line of defence. AI helps with early detection, but clear emergencies are handled by predictable, testable rules.

Raahat also supports accessibility through a phone-based IVR flow. If a person does not have a smartphone or stable internet, they can still call in, complete a check-in through keypad inputs, or trigger an SOS. The phone flow goes into the same backend, database, and risk engine as the website, so support is not limited to only app users.

On the technical side, the project uses a React frontend, FastAPI backend, MySQL database, WebSockets for real-time alerts, local ML models for scoring, and secure role-based access. Passwords are hashed, sensitive actions are audit logged, and victims cannot access anyone else's data.

We have also been honest about the prototype limits. The model is trained on synthetic data because real victim data is sensitive and cannot be casually used in a hackathon setting. Before real deployment, Raahat would need ethically sourced anonymised data, clinical validation, stronger production infrastructure, and expanded language coverage.

To conclude, Raahat is not trying to replace counsellors, courts, helplines, or emergency services. It is a decision-support system that helps the right human notice the right case at the right time.

Our goal is simple: detect distress early, protect vulnerable people better, and make support reach before crisis, not after it.

Thank you.

## Short Version (about 60-75 seconds)

Namaste judges.

Our project is **Raahat**, a distress monitoring platform for crime victims and witnesses. These individuals often experience emotional trauma during investigation, trial, or rehabilitation, but support teams may only notice the problem after it becomes serious. Raahat helps detect distress early and alerts a real counsellor before the situation escalates.

The system has three views. The victim can complete wellbeing check-ins, use a support chatbot, and press an emergency button. The counsellor sees a live dashboard where cases are prioritised by risk, with distress trends, alerts, notes, and AI explanations. The admin gets district and state-level oversight with anonymised case data.

Raahat uses two layers of detection. The first is machine learning, which combines check-in answers and chat text into a distress score from zero to one hundred. The second is a deterministic safety net for emergencies like self-harm, violence, abuse, threats, medical emergencies, and child safety concerns. This safety net works in English, Hindi, and Hinglish, and overrides the AI when urgent risk is detected.

The platform also supports phone-based IVR, so users without smartphones or internet can still check in or trigger SOS through a normal call.

Technically, Raahat uses React, FastAPI, MySQL, WebSockets, and explainable ML models. It is a prototype using synthetic data, so real deployment would need proper validation and ethically sourced data.

Raahat does not replace human counsellors. It helps them notice high-risk cases earlier, respond faster, and support vulnerable people before crisis, not after it.

Thank you.

## Delivery Cues

- Say **Raahat** slowly the first time, then continue naturally.
- Emphasise: "AI supports human judgement, it does not replace it."
- If the demo shows the chatbot emergency flow, pause after saying "deterministic safety net" and let the alert appear.
- If the recording is shorter, use the short version.
- If the recording is longer, slow down around the three user roles and the two-layer safety design.
