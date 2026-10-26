from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=50)
    email = models.CharField(max_length=100, default="")


class Profile(models.Model):
    customer = models.OneToOneField(Customer, models.CASCADE, related_name="profile")
    bio = models.CharField(max_length=100, default="")


class Category(models.Model):
    name = models.CharField(max_length=50)


class Tag(models.Model):
    name = models.CharField(max_length=50)


class Comment(models.Model):
    content_type = models.ForeignKey(ContentType, models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    text = models.CharField(max_length=100, default="")


class Product(models.Model):
    name = models.CharField(max_length=50)
    price = models.IntegerField(default=1)
    category = models.ForeignKey(Category, models.CASCADE, null=True)
    tags = models.ManyToManyField(Tag, related_name="products")
    comments = GenericRelation(Comment)


class Order(models.Model):
    customer = models.ForeignKey(Customer, models.CASCADE, related_name="orders")
    status = models.CharField(max_length=10, default="new")


class OrderItem(models.Model):
    order = models.ForeignKey(Order, models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, models.CASCADE)
    qty = models.IntegerField(default=1)
