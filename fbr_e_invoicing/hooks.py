app_name = "fbr_e_invoicing"
app_title = "FBR E-Invoicing"
app_publisher = "osama.ahmed@deliverydevs.com"
app_description = "FBR Digital invoicing integration"
app_email = "osama.ahmed@deliverydevs.com"
app_license = "mit"

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"POS Invoice": {
		"after_insert": "fbr_e_invoicing.api.pos_invoice_build_payload.get"
	},
	"Sales Invoice": {
		"validate": "fbr_e_invoicing.api.fbr_validation.validate_fbr_fields",
		"before_submit": "fbr_e_invoicing.api.fbr_validation.force_today_posting_date"
	}
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	# Process FBR queue every 15 minutes
	"cron": {
		"*/15 * * * *": [
			"fbr_e_invoicing.api.fbr_queue.process_fbr_queue_scheduled"
		]
	},
	# Cleanup old logs and queue items daily at 2 AM
	# "daily": [
	# 	"fbr_e_invoicing.api.fbr_maintenance.cleanup_old_records"
	# ],
	# Generate FBR reports weekly
	# "weekly": [
	# 	"fbr_e_invoicing.api.fbr_reports.generate_weekly_report"
	# ]
}

# Custom permissions for FBR related doctypes
# permission_query_conditions = {
# 	"FBR Queue": "fbr_e_invoicing.api.permissions.get_fbr_queue_permission_query_conditions",
# 	"FBR Logs": "fbr_e_invoicing.api.permissions.get_fbr_logs_permission_query_conditions",
# }


# Website Settings
# website_route_rules = [
# 	{"from_route": "/fbr-dashboard", "to_route": "fbr-dashboard"},
# ]

# Jinja Environment
# jinja = {
# 	"methods": [
# 		"fbr_e_invoicing.utils.jinja_methods.get_fbr_status",
# 		"fbr_e_invoicing.utils.jinja_methods.format_fbr_datetime"
# 	],
# 	"filters": [
# 		"fbr_e_invoicing.utils.jinja_filters.fbr_status_color"
# 	]
# }

# Background Jobs
# ---------------

# Override standard ERPNext methods for FBR integration
# override_whitelisted_methods = {
# 	"erpnext.accounts.doctype.sales_invoice.sales_invoice.make_sales_return": "fbr_e_invoicing.overrides.sales_invoice.make_sales_return_with_fbr"
# }

# REST API endpoints
# ------------------

# Add custom REST API endpoints
# website_generators = ["FBR Dashboard"]

# Boot session - additional info sent to client
# extend_bootinfo = [
# 	"fbr_e_invoicing.boot.boot_session"
# ]

# Custom fields that should be searchable
search_fields = {
	"Sales Invoice": ["custom_fbr_invoice_number", "custom_fbr_status"],
	"POS Invoice": ["custom_fbr_invoice_number", "custom_fbr_status"]
}

# Dashboard charts for Desk
dashboard_charts = [
	{
		"chart_name": "FBR Submissions",
		"chart_type": "Line",
		"doctype": "FBR Logs",
		"filters_json": '{"status": "Success"}',
		"source": "FBR Logs"
	},
	{
		"chart_name": "FBR Queue Status",
		"chart_type": "Donut",
		"doctype": "FBR Queue",
		"source": "FBR Queue"
	}
]

# Notifications
# --------------

# notification_config = "fbr_e_invoicing.notifications.get_notification_config"

# Email templates
# ---------------

# standard_email_templates = [
# 	{
# 		"template_name": "FBR Submission Failed",
# 		"doctype": "Sales Invoice",
# 		"subject": "FBR Submission Failed for {{ doc.name }}",
# 		"response": """
# 		<p>Dear {{ frappe.get_fullname(frappe.session.user) }},</p>
# 		<p>The FBR submission for {{ doc.doctype }} {{ doc.name }} has failed.</p>
# 		<p><strong>Error:</strong> {{ doc.custom_fbr_last_error }}</p>
# 		<p>Please review and retry the submission.</p>
# 		<p>Best regards,<br>Frappe FBR E-Invoicing System</p>
# 		"""
# 	}
# ]

# Print Formats
# --------------

# Custom print format for FBR invoices
# default_print_formats = {
# 	"Sales Invoice": "FBR Sales Invoice",
# 	"POS Invoice": "FBR POS Invoice"
# }

# Desk notifications for failed FBR submissions
# notification_config = "fbr_e_invoicing.notifications.get_notification_config"

# On app install/uninstall
# -------------------------

after_install = "fbr_e_invoicing.install.after_install"
# before_uninstall = "fbr_e_invoicing.uninstall.before_uninstall"

# Backup hook - include FBR data in backups
include_in_backup = ["FBR Logs", "FBR Queue"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "FBR Logs",
# 		"filter_by": "owner",
# 		"redact_fields": ["request_payload", "response_data"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "FBR Queue", 
# 		"filter_by": "owner",
# 		"redact_fields": ["error_message", "fbr_response"],
# 		"partial": 1,
# 	}
# ]

# Default log clearing - keep FBR logs for 90 days
# default_log_clearing_doctypes = {
# 	"FBR Logs": 90,
# 	"FBR Queue": 30  # Completed items
# }

fixtures = [
    {"dt": "Tax Category"}, 
]