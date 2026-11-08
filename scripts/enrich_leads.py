"""Enrich leads with LinkedIn profiles and suitability rationale."""

import asyncio
import os
import re
import httpx
from dotenv import load_dotenv
from harvey.state import StateManager

load_dotenv()
serper_key = os.getenv("SERPER_API_KEY")


def generate_why_fit(title: str, company: str, industry: str, email_status: str) -> str:
    t_lower = (title or "").lower()
    c_name = company or "this business"
    ind = industry if industry and not industry.startswith("(") else "Software / Digital Products"

    fit_reasons = []
    if any(k in t_lower for k in ["cto", "tech", "engineer"]):
        fit_reasons.append("Technical decision-maker who manages architecture and capacity bottlenecks.")
    elif any(k in t_lower for k in ["founder", "ceo", "owner", "president", "partner"]):
        fit_reasons.append("Executive decision-maker with direct authority to greenlight milestone-based development engagements.")
    else:
        fit_reasons.append("Leadership contact within the organization.")

    if any(k in (c_name + " " + ind).lower() for k in ["agency", "studio", "design"]):
        fit_reasons.append("Digital agency/studio model: frequently needs a reliable backend implementation partner for client web apps and APIs.")
    else:
        fit_reasons.append("Growing SaaS/tech product: core team focuses on flagship features, creating backlogs for integrations, internal tools, and secondary APIs.")

    reach = (
        "Deliverable email (ready for email outreach or CRM import)"
        if email_status in ("verified", "risky", "catch-all")
        else "Guessed email (recommended to connect via LinkedIn or website contact form)"
    )

    return (
        f"Role Match: {fit_reasons[0]}\n"
        f"Product Fit: {fit_reasons[1]}\n"
        f"MeritHQ Angle: Fixed-scope sprint for backend APIs, integrations, or MVP builds without hiring overhead.\n"
        f"Reachability: {reach}"
    )


async def enrich_all():
    state = StateManager()
    await state.init_db()
    async with state._connect() as db:
        async with db.execute(
            "SELECT id, first_name, last_name, title, company, industry, email, email_status, linkedin_url FROM prospects"
        ) as cursor:
            prospects = await cursor.fetchall()

    print(f"Enriching {len(prospects)} prospects...")
    count_enriched = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for p in prospects:
            pid, fn, ln, title, company, industry, email, estatus, li_url = p
            name = f"{fn} {ln}".strip()

            # 1. Search LinkedIn if missing
            if not li_url and serper_key:
                q = f"{name} {company} linkedin" if name else f"{company} linkedin company"
                try:
                    resp = await client.post(
                        "https://google.serper.dev/search",
                        headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                        json={"q": q},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("organic", []):
                            link = item.get("link", "")
                            if "linkedin.com/in/" in link or ("linkedin.com/company/" in link and not name):
                                li_url = link.split("?")[0].rstrip("/")
                                break
                except Exception as e:
                    print(f"Error searching for {name or company}: {e}")

            # 2. Generate Why Lead Rationale
            why_fit = generate_why_fit(title, company, industry, estatus)

            # 3. Update DB
            async with state._connect() as db:
                await db.execute(
                    "UPDATE prospects SET linkedin_url = ?, personalization_notes = ? WHERE id = ?",
                    (li_url or "", why_fit, pid),
                )
                await db.commit()

            display_name = name if name else "General Inbox"
            li_display = li_url if li_url else "Not found"
            print(f"✓ {display_name} ({company}) -> {li_display}")
            count_enriched += 1

    print(f"\nEnrichment complete! Successfully updated {count_enriched} prospects.")


if __name__ == "__main__":
    asyncio.run(enrich_all())
