from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    category = models.ForeignKey(Category, models.PROTECT, related_name="products")

    def __str__(self):
        return self.name


class Order(models.Model):
    customer = models.ForeignKey(Customer, models.CASCADE, related_name="orders")
    created = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="paid")

    def __str__(self):
        return f"Order #{self.pk}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity}× {self.product_id}"


class Review(models.Model):
    """A big table: `seed` writes ~400k rows. `body` and `helpful` have no index."""

    product = models.ForeignKey(Product, models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField()
    helpful = models.PositiveIntegerField(default=0)
    body = models.TextField()

    def __str__(self):
        return f"Review #{self.pk} ({self.rating}★)"
