"""
amc_templates.py — the AMC reminder ladder.

One source of truth for both halves of the job: `scripts/submit_whatsapp_templates.py`
submits these to Meta for approval, and `amc_reminders.py` sends them. Keep them in
step — a template whose body here differs from the approved one on Meta's side still
sends, but the {{n}} slots silently land in the wrong places.

`days` is days *until* expiry: positive is before, negative is after. A customer is
messaged only on the days listed here — never daily. Six touches over 45 days is the
whole lifecycle.

Categories are not cosmetic. UTILITY is about a service the customer already holds:
cheaper, delivered without marketing opt-in, and not counted against Meta's per-user
marketing cap. The moment copy promotes a discount it becomes MARKETING, which needs
opt-in and burns that cap. So the discount appears in three of six touches, not all six.
"""

from typing import Dict, List, Optional

# Every body takes the same three parameters, in this order.
#   {{1}} customer first name
#   {{2}} product / asset the contract covers
#   {{3}} expiry date, written out (e.g. "25 September 2026")
BODY_PARAMS = ("customer_name", "product", "expiry_date")

# Templates that quote a discount take it as a parameter rather than baking it into
# the copy. A hardcoded "10 percent" needs a delete-and-resubmit every time the
# campaign changes, and Meta re-reviews from scratch each time — so the number the
# customer sees is whatever was approved weeks ago, not what is running today.
OFFER_PARAMS = ("customer_name", "product", "expiry_date", "discount_pct")

LANGUAGE = "en"

# Names carry a version suffix because Meta reserves a deleted template's name for
# four weeks — "New English content can't be added while the existing English
# content is being deleted." Changing approved copy therefore means a NEW name, not
# a redelete. Never delete a template you intend to replace; create the successor
# first, cut over, then delete the old one.

# Marketing templates must give a way out. Meta reads this footer as the opt-out
# affordance; `amc_reminders.OPT_OUT_WORDS` is what actually honours it.
OPT_OUT_FOOTER = "Reply STOP to stop these reminders."


class Template:
    def __init__(
        self,
        name: str,
        days: int,
        category: str,
        body: str,
        footer: Optional[str] = None,
        label: str = "",
        params: tuple = BODY_PARAMS,
        example: Optional[list] = None,
    ):
        self.name = name
        self.days = days
        self.category = category
        self.body = body
        self.footer = footer
        self.label = label
        self.params = params
        self.example = example or ["Ramesh", "Aquaguard Marvel", "25 September 2026"]

    @property
    def is_marketing(self) -> bool:
        return self.category == "MARKETING"

    def meta_payload(self) -> dict:
        """The create-template request body for the WhatsApp Business Management API."""
        components: List[dict] = [{
            "type": "BODY",
            "text": self.body,
            "example": {"body_text": [self.example]},
        }]
        if self.footer:
            components.append({"type": "FOOTER", "text": self.footer})
        return {
            "name": self.name,
            "language": LANGUAGE,
            "category": self.category,
            "components": components,
        }

    def __repr__(self) -> str:
        return f"<Template {self.name} d{self.days:+d} {self.category}>"


LADDER: List[Template] = [
    Template(
        name="amc_expiry_30d",
        days=30,
        category="UTILITY",
        label="30 days before expiry",
        body=(
            "Hi {{1}}, a reminder from Eureka Forbes. Your AMC for {{2}} is due to "
            "expire on {{3}}. Renewing before then keeps your scheduled service "
            "visits and priority support running without a break. Reply here and our "
            "team will take it forward."
        ),
    ),
    Template(
        name="amc_expiry_15d",
        days=15,
        category="UTILITY",
        label="15 days before expiry",
        body=(
            "Hi {{1}}, your Eureka Forbes AMC for {{2}} expires on {{3}}, which is "
            "about two weeks away. Reply here if you would like us to start the "
            "renewal for you."
        ),
    ),
    Template(
        name="amc_renewal_offer_7d_v2",
        days=7,
        category="MARKETING",
        label="7 days before expiry — offer",
        body=(
            "Hi {{1}}, your Eureka Forbes AMC for {{2}} expires on {{3}}. Renew this "
            "week and you get {{4}}% off the renewal. Reply here and our team "
            "will confirm the plan and the price for you."
        ),
        footer=OPT_OUT_FOOTER,
        params=OFFER_PARAMS,
        example=["Ramesh", "Aquaguard Marvel", "25 September 2026", "20"],
    ),
    Template(
        name="amc_expiry_tomorrow",
        days=1,
        category="UTILITY",
        label="day before expiry",
        body=(
            "Hi {{1}}, your Eureka Forbes AMC for {{2}} expires tomorrow, {{3}}. "
            "Once it lapses, service visits and parts are billed separately. Reply "
            "here and we can get the renewal done for you today."
        ),
    ),
    Template(
        name="amc_lapsed_offer_v2",
        days=-3,
        category="MARKETING",
        label="3 days after expiry — reactivation offer",
        body=(
            "Hi {{1}}, your Eureka Forbes AMC for {{2}} lapsed on {{3}}. You can "
            "still reactivate it, and the {{4}}% discount is open to you. Reply "
            "here and we will get it restarted."
        ),
        footer=OPT_OUT_FOOTER,
        params=OFFER_PARAMS,
        example=["Ramesh", "Aquaguard Marvel", "25 September 2026", "20"],
    ),
    Template(
        name="amc_lapsed_final_v2",
        days=-15,
        category="MARKETING",
        label="15 days after expiry — final touch",
        body=(
            "Hi {{1}}, this is our last reminder about the Eureka Forbes AMC for {{2}} "
            "that lapsed on {{3}}. The {{4}}% reactivation offer is still open if "
            "you would like to use it. Reply here anytime and we will help."
        ),
        footer=OPT_OUT_FOOTER,
        params=OFFER_PARAMS,
        example=["Ramesh", "Aquaguard Marvel", "25 September 2026", "20"],
    ),
]

BY_DAYS: Dict[int, Template] = {t.days: t for t in LADDER}
BY_NAME: Dict[str, Template] = {t.name: t for t in LADDER}

# The only days the job ever sends on.
MILESTONE_DAYS: List[int] = sorted(BY_DAYS.keys(), reverse=True)


def for_days(days: int) -> Optional[Template]:
    return BY_DAYS.get(days)


# ── Campaign broadcasts ──────────────────────────────────────────────────────
# Not part of the expiry ladder: these go to a list, not to whoever happens to sit
# on a milestone today. `days` is unused and set to 0.
#
# The discount and the end date are parameters, so the same approved template can
# carry next quarter's campaign without another review cycle. Meta forbids reusing
# one variable twice in a body, which is why the copy says "everything" once rather
# than repeating the percentage for products and for AMC.

CAMPAIGN_PARAMS = ("customer_name", "discount_pct", "ends_on")

CAMPAIGNS: List[Template] = [
    Template(
        name="ef_monsoon_offer_customer",
        days=0,
        category="MARKETING",
        label="monsoon campaign — existing customers",
        body=(
            "Hi {{1}}, our monsoon offer is now on at Eureka Forbes — {{2}}% off "
            "everything: every product, every range, and AMC renewals too. You also "
            "get a free pre-monsoon check-up for your appliance. Valid until {{3}}. "
            "Reply here and our team will set it up for you."
        ),
        footer=OPT_OUT_FOOTER,
        params=CAMPAIGN_PARAMS,
        example=["Ramesh", "20", "8 September"],
    ),
    Template(
        name="ef_monsoon_offer_general",
        days=0,
        category="MARKETING",
        label="monsoon campaign — leads and prospects",
        body=(
            "Hi {{1}}, Eureka Forbes has {{2}}% off everything this monsoon — water "
            "purifiers, air purifiers and vacuum cleaners, across every range. Offer "
            "is open until {{3}}. Reply here and we will send you the details."
        ),
        footer=OPT_OUT_FOOTER,
        params=CAMPAIGN_PARAMS,
        example=["Ramesh", "20", "8 September"],
    ),
]

ALL_TEMPLATES: List[Template] = LADDER + CAMPAIGNS
BY_NAME.update({c.name: c for c in CAMPAIGNS})
