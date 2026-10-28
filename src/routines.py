"""Routines: ready-made scheduled tasks, one tap each (the Tasks window's
Routines tab, Flask/templates/schedule.html).

Each one is an ordinary scheduled task with a well-written prompt: picking
it adds a line to ping.md through /api/schedule, like any other task, so
it's listed under Scheduled and can be changed or removed there too. Its
action starts "Routine: <title>." — how the window knows which are on, and
how Activity (src/activity.py) names a run. Titles therefore have no full
stop in them.

A routine is daily or weekly, at a time the person can change (weekly ones
on a day they can change too). One with an "input" asks for a single detail
first, like what a price watch should track, and the prompt gets it in place
of {input}. A host can add to ROUTINES, or replace it.
"""

# Weekly days: 0 Monday … 6 Sunday.
ROUTINES = [
    # ── Home ──
    {"key": "morning", "cat": "Home", "title": "Morning briefing", "repeat": "daily", "time": "07:00",
     "blurb": "Weather, today's calendar, and what in your inbox needs you.", "needs": ["Gmail", "Calendar"],
     "prompt": "Give me my morning briefing for today. Include the weather where I live (if you don't "
               "know where that is, ask me once and remember it), today's events from my calendar, and "
               "anything in my inbox from the last day that needs me. If Gmail or Google Calendar isn't "
               "connected, skip that part and say in one line how to connect it in the App Store. Keep it "
               "to a short list I can read in 30 seconds."},
    {"key": "weekahead", "cat": "Home", "title": "Week ahead", "repeat": "weekly", "day": 6, "time": "18:00",
     "blurb": "Every Sunday: appointments, bills due and deadlines for the next 7 days.",
     "needs": ["Gmail", "Calendar"],
     "prompt": "Send me the week ahead for the next 7 days: appointments and events from my calendar, "
               "bills or payments due, deadlines, and anything from school or work I need to prepare "
               "for, found in my email. Group it by day. Point out anything that clashes or needs doing "
               "beforehand. If my calendar or email isn't connected, say how to connect it in the App "
               "Store and work with what you have."},
    {"key": "bills", "cat": "Home", "title": "Bill reminders", "repeat": "weekly", "day": 0, "time": "09:00",
     "blurb": "Every Monday: bills and payments due in the next two weeks.", "needs": ["Gmail"],
     "prompt": "Check my email for bills, statements and payment reminders due in the next 14 days. "
               "List each with who it's from, the amount and the due date, soonest first, and flag "
               "anything overdue. Don't pay anything or reply to anyone. If you find none, say so in "
               "one line."},
    {"key": "deliveries", "cat": "Home", "title": "Deliveries", "repeat": "daily", "time": "17:00",
     "blurb": "What's arriving, what's late, and anything that needs you.", "needs": ["Gmail"],
     "prompt": "Look through my email for shipping and delivery updates from the last two days. Tell me "
               "what's arriving and when, what's delayed, and anything that needs me (a signature, a "
               "missed delivery, a return window closing). If nothing changed, reply with one line."},
    # ── Money ──
    {"key": "subscriptions", "cat": "Money", "title": "Subscription check", "repeat": "weekly", "day": 5,
     "time": "10:00", "blurb": "Every Saturday: new charges, price increases and trials about to bill you.",
     "needs": ["Gmail"],
     "prompt": "Find receipts, renewals and subscription emails from the past 7 days. List each charge "
               "with the service and the amount, and flag new subscriptions, price increases and free "
               "trials about to turn into paid ones. Don't cancel or change anything."},
    {"key": "pricewatch", "cat": "Money", "title": "Price watch", "repeat": "daily", "time": "08:00",
     "blurb": "Checks the price every morning and tells you when it drops.", "needs": ["Browser"],
     "input": {"label": "What to watch, and your price", "placeholder": "Sony WH-1000XM6 headphones, under $300"},
     "prompt": "Check the current price of this at the major stores, using your browser: {input}. Tell "
               "me the lowest price you find, where, and a link. If it's at or under my target price, "
               "start your reply with \"Price drop:\". Keep it to three lines."},
    # ── Work ──
    {"key": "triage", "cat": "Work", "title": "Inbox triage", "repeat": "daily", "time": "08:00",
     "blurb": "Sorts what came in and drafts replies to the important ones.", "needs": ["Gmail"],
     "prompt": "Go through my unread email from the last day. Sort it into: needs my reply, needs me "
               "to do something, FYI, and can ignore. For the three most important, draft a reply I "
               "can send (save them as drafts if you can, otherwise show them here). Don't send anything."},
    {"key": "meetingprep", "cat": "Work", "title": "Meeting prep", "repeat": "daily", "time": "07:30",
     "blurb": "Who's coming, what it's about, and what to prepare.", "needs": ["Calendar", "Gmail"],
     "prompt": "Look at today's meetings on my calendar. For each one: when, who's coming, what it's "
               "about, and anything relevant from recent emails with those people. End with what I "
               "should prepare. If I have no meetings today, reply with one line."},
    {"key": "followups", "cat": "Work", "title": "Follow-ups", "repeat": "daily", "time": "15:00",
     "blurb": "Emails you sent that never got an answer, with a nudge for each.", "needs": ["Gmail"],
     "prompt": "Find emails I sent in the past 10 days that haven't had a reply and probably need one. "
               "List them, oldest first, with a one-line follow-up I could send for each. Don't send "
               "anything. If there are none, reply with one line."},
    {"key": "wrapup", "cat": "Work", "title": "Weekly wrap-up", "repeat": "weekly", "day": 4, "time": "16:00",
     "blurb": "Every Friday: what got done, what's open, and next week's top three.",
     "needs": ["Gmail", "Calendar"],
     "prompt": "Write my weekly wrap-up from my email and calendar this week: what got done, decisions "
               "made, what's still open, and the top three things for next week. Keep it short enough "
               "to paste into a status update."},
    # ── School & family ──
    {"key": "school", "cat": "School & family", "title": "School digest", "repeat": "daily", "time": "16:00",
     "blurb": "Forms, events, money to send in and messages from teachers.", "needs": ["Gmail"],
     "prompt": "Check today's emails from school, teachers and school apps. Tell me about events, forms "
               "or permission slips due, money to send in, grades or messages that need a reply, and "
               "anything coming up this week. If there's nothing new, say so in one line."},
    {"key": "study", "cat": "School & family", "title": "Study plan", "repeat": "weekly", "day": 6,
     "time": "17:00", "blurb": "Every Sunday: a plan for the week around tests and deadlines.",
     "needs": ["Calendar", "Gmail"],
     "prompt": "Look at my calendar and email for tests, assignments and deadlines in the next two "
               "weeks. Make a day-by-day study plan for this week that fits around my other events, "
               "most urgent first."},
    {"key": "weekend", "cat": "School & family", "title": "Weekend ideas", "repeat": "weekly", "day": 3,
     "time": "18:00", "blurb": "Every Thursday: five things to do this weekend, with the forecast.",
     "needs": ["Browser"], "input": {"label": "Where", "placeholder": "Chicago, IL"},
     "prompt": "Find 5 things to do this weekend in or near {input}: events, markets, shows, outdoor "
               "plans and something free. Include the weather forecast for Saturday and Sunday, and "
               "times, prices and links."},
    # ── News & business ──
    {"key": "news", "cat": "News & business", "title": "News digest", "repeat": "daily", "time": "07:30",
     "blurb": "The day's top stories on a topic you choose.", "needs": ["Browser"],
     "input": {"label": "Topic", "placeholder": "electric cars, the Cubs, AI regulation"},
     "prompt": "Find the most important news from the last 24 hours about {input}. Give me the top 5 "
               "stories, one or two sentences each, with the source and a link. Skip anything you've "
               "already told me about."},
    {"key": "reviews", "cat": "News & business", "title": "Review watch", "repeat": "daily", "time": "09:00",
     "blurb": "New reviews of your business, with a reply drafted for each.", "needs": ["Browser"],
     "input": {"label": "Your business", "placeholder": "Rosa's Bakery, Austin TX"},
     "prompt": "Check for new customer reviews of {input} on Google, Yelp and other review sites from "
               "the last day. Summarize what people said, flag anything negative, and draft a short, "
               "friendly reply to each new review. Don't post anything."},
    {"key": "competitors", "cat": "News & business", "title": "Competitor watch", "repeat": "weekly",
     "day": 0, "time": "09:00", "blurb": "Every Monday: what changed on competitors' sites.",
     "needs": ["Browser"], "input": {"label": "Who to watch", "placeholder": "acme.com, examplecorp.com"},
     "prompt": "Check these competitors' websites: {input}. Tell me what changed in the past week: new "
               "products, prices, promotions, announcements or job postings, and anything I should "
               "respond to."},
]

ACTION_MAX = 2000


def action(routine, value=""):
    """The ping.md action for a routine: what the agent is asked each time."""
    text = routine["prompt"].replace("{input}", " ".join(str(value or "").split())[:200])
    return f"Routine: {routine['title']}. {text}"[:ACTION_MAX]


def catalog():
    """ROUTINES as the window gets them."""
    keys = ("key", "cat", "title", "blurb", "repeat", "time", "day", "needs", "input", "prompt")
    return [{k: r[k] for k in keys if k in r} for r in ROUTINES]
