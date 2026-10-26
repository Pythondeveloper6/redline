"""Create demo data: customers, products, orders and ~400k reviews, plus admin/admin."""

import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from shop.models import Category, Customer, Order, OrderItem, Product, Review

WORDS = (
    "great battery screen fast slow cheap sturdy broke love hate value shipping box "
    "quality colour size fits returned gift works perfect noisy quiet light heavy"
).split()


class Command(BaseCommand):
    help = "Seed the demo shop (idempotent: wipes shop data first)."

    def add_arguments(self, parser):
        parser.add_argument("--reviews", type=int, default=400_000)

    @transaction.atomic
    def handle(self, *args, reviews, **options):
        rng = random.Random(42)
        for model in (Review, OrderItem, Order, Product, Category, Customer):
            model.objects.all().delete()

        categories = Category.objects.bulk_create(
            Category(name=n) for n in ("Phones", "Laptops", "Audio", "Cameras", "Games")
        )
        products = Product.objects.bulk_create(
            Product(
                name=f"{rng.choice(WORDS).title()} {cat.name[:-1]} {i}",
                price=rng.randint(10, 2000),
                category=cat,
            )
            for i, cat in enumerate(rng.choice(categories) for _ in range(60))
        )
        customers = Customer.objects.bulk_create(
            Customer(name=f"Customer {i}", email=f"customer{i}@example.com") for i in range(80)
        )
        orders = Order.objects.bulk_create(
            Order(customer=rng.choice(customers)) for _ in range(300)
        )
        OrderItem.objects.bulk_create(
            OrderItem(order=o, product=rng.choice(products), quantity=rng.randint(1, 4))
            for o in orders
            for _ in range(rng.randint(1, 4))
        )
        batch = []
        for _ in range(reviews):
            body = " ".join(rng.choice(WORDS) for _ in range(12))
            batch.append(
                Review(
                    product=rng.choice(products),
                    rating=rng.randint(1, 5),
                    helpful=rng.randint(0, 500),
                    body=body,
                )
            )
            if len(batch) >= 5000:
                Review.objects.bulk_create(batch)
                batch.clear()
        Review.objects.bulk_create(batch)

        User = get_user_model()
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@example.com", "admin")
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(products)} products, {len(customers)} customers, "
                f"{len(orders)} orders, {reviews} reviews. Superuser: admin / admin (demo only)."
            )
        )
