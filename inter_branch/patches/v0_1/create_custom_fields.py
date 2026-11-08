"""Create the app's Custom Fields on an existing site."""

from inter_branch.setup.custom_fields import ensure_branch_columns, setup_custom_fields


def execute():
	setup_custom_fields()
	ensure_branch_columns()
