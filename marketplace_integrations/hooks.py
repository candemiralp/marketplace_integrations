app_name = "marketplace_integrations"
app_title = "Marketplace Integrations"
app_publisher = "Can Demiralp"
app_description = "Integrates with marketplaces"
app_email = "github@candemiralp.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "marketplace_integrations",
# 		"logo": "/assets/marketplace_integrations/logo.png",
# 		"title": "Marketplace Integrations",
# 		"route": "/marketplace_integrations",
# 		"has_permission": "marketplace_integrations.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/marketplace_integrations/css/marketplace_integrations.css"
# app_include_js = "/assets/marketplace_integrations/js/marketplace_integrations.js"

# include js, css files in header of web template
# web_include_css = "/assets/marketplace_integrations/css/marketplace_integrations.css"
# web_include_js = "/assets/marketplace_integrations/js/marketplace_integrations.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "marketplace_integrations/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "marketplace_integrations/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "marketplace_integrations.utils.jinja_methods",
# 	"filters": "marketplace_integrations.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "marketplace_integrations.install.before_install"
# after_install = "marketplace_integrations.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "marketplace_integrations.uninstall.before_uninstall"
# after_uninstall = "marketplace_integrations.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "marketplace_integrations.utils.before_app_install"
# after_app_install = "marketplace_integrations.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "marketplace_integrations.utils.before_app_uninstall"
# after_app_uninstall = "marketplace_integrations.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "marketplace_integrations.build.after_build"

# To hook into the build process of other apps
# The list of apps being built is passed as an argument

# after_app_build = "marketplace_integrations.build.after_app_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "marketplace_integrations.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["marketplace_integrations.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"marketplace_integrations.tasks.all"
# 	],
# 	"daily": [
# 		"marketplace_integrations.tasks.daily"
# 	],
# 	"hourly": [
# 		"marketplace_integrations.tasks.hourly"
# 	],
# 	"weekly": [
# 		"marketplace_integrations.tasks.weekly"
# 	],
# 	"monthly": [
# 		"marketplace_integrations.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "marketplace_integrations.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "marketplace_integrations.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "marketplace_integrations.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["marketplace_integrations.utils.before_request"]
# after_request = ["marketplace_integrations.utils.after_request"]

# Job Events
# ----------
# before_job = ["marketplace_integrations.utils.before_job"]
# after_job = ["marketplace_integrations.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"marketplace_integrations.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

