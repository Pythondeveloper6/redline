"""Create shop.db with enough rows for the demo (a couple of seconds)."""

from __future__ import annotations

import random

from shop.db import DB_FILE, sync_engine
from shop.models import AuditLog, Base, Customer, Order, OrderItem, Product, Tag, order_tags
from sqlalchemy import insert


def main(audit_rows: int = 400_000) -> None:
    rng = random.Random(7)
    DB_FILE.unlink(missing_ok=True)
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        conn.execute(
            insert(Customer),
            [{"name": f"Customer {i}", "email": f"c{i}@example.com"} for i in range(1, 61)],
        )
        conn.execute(
            insert(Product),
            [
                {"name": f"Product {i}", "price": round(rng.uniform(3, 99), 2)}
                for i in range(1, 201)
            ],
        )
        conn.execute(insert(Tag), [{"name": n} for n in ("gift", "rush", "wholesale", "promo")])
        conn.execute(
            insert(Order),
            [{"customer_id": rng.randint(1, 60), "status": "paid"} for _ in range(300)],
        )
        conn.execute(
            insert(OrderItem),
            [
                {"order_id": o, "product_id": rng.randint(1, 200), "quantity": rng.randint(1, 4)}
                for o in range(1, 301)
                for _ in range(rng.randint(2, 6))
            ],
        )
        conn.execute(
            insert(order_tags),
            [
                {"order_id": o, "tag_id": t}
                for o in range(1, 301)
                for t in rng.sample(range(1, 5), rng.randint(1, 3))
            ],
        )
        words = ["login", "refund", "export", "update", "delete", "view", "invoice", "retry"]
        conn.execute(
            insert(AuditLog),
            [
                {
                    "actor": f"user{rng.randint(1, 500)}",
                    "message": f"{rng.choice(words)} {rng.choice(words)} ref-{i} "
                    + "lorem ipsum dolor sit amet " * 2,
                }
                for i in range(audit_rows)
            ],
        )
    print(f"seeded {DB_FILE} ({audit_rows} audit rows)")


if __name__ == "__main__":
    main()
