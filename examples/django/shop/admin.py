from django.contrib import admin

from .models import Category, Customer, Order, OrderItem, Product

admin.site.register([Category, Customer, Order, OrderItem, Product])
