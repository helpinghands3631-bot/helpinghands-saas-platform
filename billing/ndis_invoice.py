"""NDIS Invoice Generator - auto-generates invoices from completed shifts"""
import os
import csv
import io
from datetime import datetime, date
from typing import Optional
from dataclasses import dataclass, field
from notion_client import AsyncClient as NotionClient
import httpx

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_SHIFTS_DB = os.getenv("NOTION_SHIFTS_DB", "")
NOTION_CLIENTS_DB = os.getenv("NOTION_CLIENTS_DB", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ABN = os.getenv("ABN", "65681861276")
BUSINESS_NAME = "Helping Hands Support Services"

notion = NotionClient(auth=NOTION_TOKEN)

SUPPORT_RATES = {
    "Daily Activities": 67.56,
    "Community Participation": 67.56,
    "Capacity Building": 72.12,
    "Transport": 0.85,  # per km
    "Short Term Accommodation": 371.00,
    "Assistance with Social": 67.56,
}


@dataclass
class InvoiceLineItem:
    date: str
    support_type: str
    hours: float
    rate: float
    kilometres: float = 0.0
    total: float = field(init=False)

    def __post_init__(self):
        transport_cost = self.kilometres * SUPPORT_RATES.get("Transport", 0.85)
        self.total = (self.hours * self.rate) + transport_cost


@dataclass
class Invoice:
    invoice_number: str
    client_name: str
    ndis_number: str
    plan_manager: str
    invoice_date: str
    line_items: list[InvoiceLineItem]
    subtotal: float = field(init=False)
    gst: float = field(init=False)
    total: float = field(init=False)

    def __post_init__(self):
        self.subtotal = sum(item.total for item in self.line_items)
        self.gst = 0.0  # NDIS services are GST-free
        self.total = self.subtotal


class NDISInvoiceGenerator:
    """Generates NDIS-compliant invoices from Notion shift data."""

    async def get_completed_shifts(self, client_name: str, month: int, year: int):
        """Fetch completed shifts for a client in a given month."""
        try:
            start = date(year, month, 1).isoformat()
            if month == 12:
                end = date(year + 1, 1, 1).isoformat()
            else:
                end = date(year, month + 1, 1).isoformat()

            resp = await notion.databases.query(
                database_id=NOTION_SHIFTS_DB,
                filter={
                    "and": [
                        {"property": "Client", "rich_text": {"equals": client_name}},
                        {"property": "Status", "select": {"equals": "Completed"}},
                        {"property": "Date", "date": {"on_or_after": start}},
                        {"property": "Date", "date": {"before": end}}
                    ]
                }
            )
            return resp.get("results", [])
        except Exception as e:
            print(f"Error fetching shifts: {e}")
            return []

    def parse_shift(self, page: dict) -> Optional[InvoiceLineItem]:
        """Parse a Notion shift page into an InvoiceLineItem."""
        try:
            props = page["properties"]
            date_val = props["Date"]["date"]["start"]
            support_type = props["Support Type"]["select"]["name"]
            start_t = props["Start Time"]["rich_text"][0]["plain_text"]
            end_t = props.get("Actual End", props["End Time"])["rich_text"][0]["plain_text"]
            km = props.get("Kilometres Actual", {}).get("number", 0) or 0

            # Calculate hours
            fmt = "%H:%M"
            start_dt = datetime.strptime(start_t, fmt)
            end_dt = datetime.strptime(end_t, fmt)
            hours = (end_dt - start_dt).seconds / 3600

            rate = SUPPORT_RATES.get(support_type, 67.56)
            return InvoiceLineItem(date=date_val, support_type=support_type, hours=hours, rate=rate, kilometres=km)
        except Exception as e:
            print(f"Parse error: {e}")
            return None

    def generate_invoice(self, client_name: str, ndis_number: str, plan_manager: str,
                         line_items: list[InvoiceLineItem], month: int, year: int) -> Invoice:
        """Create Invoice object."""
        inv_num = f"HH-{year}{month:02d}-{client_name[:3].upper()}"
        return Invoice(
            invoice_number=inv_num,
            client_name=client_name,
            ndis_number=ndis_number,
            plan_manager=plan_manager,
            invoice_date=datetime.utcnow().strftime("%Y-%m-%d"),
            line_items=line_items
        )

    def to_csv(self, invoice: Invoice) -> str:
        """Export invoice as CSV for NDIS portal upload."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Date", "Support Item", "Hours", "Rate (AUD)", "Kilometres", "Total (AUD)"])
        for item in invoice.line_items:
            writer.writerow([item.date, item.support_type, f"{item.hours:.2f}",
                             f"{item.rate:.2f}", f"{item.kilometres:.1f}", f"{item.total:.2f}"])
        writer.writerow([])
        writer.writerow(["SUBTOTAL", "", "", "", "", f"${invoice.subtotal:.2f}"])
        writer.writerow(["GST (0%)", "", "", "", "", "$0.00"])
        writer.writerow(["TOTAL", "", "", "", "", f"${invoice.total:.2f}"])
        return output.getvalue()

    def to_text(self, invoice: Invoice) -> str:
        """Generate readable invoice text."""
        lines = [
            f"TAX INVOICE",
            f"{BUSINESS_NAME}",
            f"ABN: {ABN}",
            f"Invoice #: {invoice.invoice_number}",
            f"Date: {invoice.invoice_date}",
            f"",
            f"Bill To: {invoice.plan_manager}",
            f"Participant: {invoice.client_name}",
            f"NDIS #: {invoice.ndis_number}",
            f"",
            f"{'Date':<12} {'Support Type':<30} {'Hrs':>5} {'Rate':>8} {'Total':>10}",
            "-" * 70
        ]
        for item in invoice.line_items:
            lines.append(f"{item.date:<12} {item.support_type:<30} {item.hours:>5.2f} ${item.rate:>7.2f} ${item.total:>9.2f}")
        lines.extend([
            "-" * 70,
            f"{'TOTAL (GST-Free)':<49} ${invoice.total:>9.2f}"
        ])
        return "\n".join(lines)

    async def run_monthly(self, month: int = None, year: int = None):
        """Generate invoices for all clients for a given month."""
        now = datetime.utcnow()
        month = month or now.month
        year = year or now.year

        # Get all active clients
        clients_resp = await notion.databases.query(
            database_id=NOTION_CLIENTS_DB,
            filter={"property": "Status", "select": {"equals": "Active"}}
        )
        clients = clients_resp.get("results", [])
        print(f"Generating invoices for {len(clients)} clients - {month}/{year}")

        for client_page in clients:
            props = client_page["properties"]
            name = props["Name"]["title"][0]["plain_text"]
            ndis_num = props.get("NDIS Number", {}).get("rich_text", [{}])[0].get("plain_text", "")
            plan_mgr = props.get("Plan Manager", {}).get("rich_text", [{}])[0].get("plain_text", "")

            shifts = await self.get_completed_shifts(name, month, year)
            if not shifts:
                continue

            items = [self.parse_shift(s) for s in shifts]
            items = [i for i in items if i]
            if not items:
                continue

            invoice = self.generate_invoice(name, ndis_num, plan_mgr, items, month, year)
            print(self.to_text(invoice))
            print(f"Invoice {invoice.invoice_number}: ${invoice.total:.2f}")


if __name__ == "__main__":
    import asyncio
    gen = NDISInvoiceGenerator()
    asyncio.run(gen.run_monthly())
