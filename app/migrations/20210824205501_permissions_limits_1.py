# Generated by database migrator
import peewee


def migrate(migrator, database, **kwargs):
    migrator.add_columns(
        "user_crafty", limit_user_creation=peewee.IntegerField(default=0)
    )
    migrator.add_columns(
        "user_crafty", limit_role_creation=peewee.IntegerField(default=0)
    )
    migrator.add_columns("user_crafty", created_server=peewee.IntegerField(default=0))
    migrator.add_columns("user_crafty", created_user=peewee.IntegerField(default=0))
    migrator.add_columns("user_crafty", created_role=peewee.IntegerField(default=0))
    """
    Write your migrations here.
    """


def rollback(migrator, database, **kwargs):
    migrator.drop_columns("user_crafty", ["limit_user_creation"])
    migrator.drop_columns("user_crafty", ["limit_role_creation"])
    migrator.drop_columns("user_crafty", ["created_server"])
    migrator.drop_columns("user_crafty", ["created_user"])
    migrator.drop_columns("user_crafty", ["created_role"])
    """
    Write your rollback migrations here.
    """
