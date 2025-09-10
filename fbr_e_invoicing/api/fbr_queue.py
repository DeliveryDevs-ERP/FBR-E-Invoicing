import frappe
import json
from datetime import datetime, timedelta
from frappe.utils import now, add_to_date, get_datetime

@frappe.whitelist()
def add_to_queue(doctype, docname, status="Pending", error_message="", priority=5):
    """Add a document to the FBR queue"""
    try:
        # Check if already in queue
        existing = frappe.db.exists("FBR Queue", {
            "document_type": doctype,
            "document_name": docname,
            "status": ["in", ["Pending", "Processing"]]
        })
        
        if existing:
            # Update existing queue item
            queue_doc = frappe.get_doc("FBR Queue", existing)
            queue_doc.status = status
            queue_doc.error_message = error_message
            queue_doc.retry_count = (queue_doc.retry_count or 0) + 1
            queue_doc.last_retry_at = now()
            queue_doc.save(ignore_permissions=True)
        else:
            # Create new queue item
            queue_doc = frappe.new_doc("FBR Queue")
            queue_doc.update({
                "document_type": doctype,
                "document_name": docname,
                "status": status,
                "priority": priority,
                "error_message": error_message,
                "retry_count": 0,
                "created_at": now()
            })
            queue_doc.insert(ignore_permissions=True)
        
        frappe.db.commit()
        return {"success": True, "queue_id": queue_doc.name}
        
    except Exception as e:
        frappe.log_error(f"Error adding to FBR queue: {str(e)}", "FBR Queue")
        return {"success": False, "error": str(e)}

@frappe.whitelist()
def process_queue(limit=50):
    """Process pending items in the FBR queue"""
    try:
        # Get pending queue items
        queue_items = frappe.get_all(
            "FBR Queue",
            filters={
                "status": "Pending",
                "retry_count": ["<", 5]  # Max 5 retries
            },
            fields=["name", "document_type", "document_name", "priority", "retry_count"],
            order_by="priority desc, created_at asc",
            limit=limit
        )
        
        processed_count = 0
        
        for item in queue_items:
            try:
                # Mark as processing
                frappe.db.set_value("FBR Queue", item.name, "status", "Processing")
                frappe.db.commit()
                
                # Process the item
                result = process_queue_item(item)
                
                if result["success"]:
                    # Mark as completed
                    frappe.db.set_value("FBR Queue", item.name, {
                        "status": "Completed",
                        "completed_at": now(),
                        "error_message": ""
                    })
                    processed_count += 1
                else:
                    # Mark as failed or pending for retry
                    retry_count = item.retry_count + 1
                    if retry_count >= 5:
                        status = "Failed"
                    else:
                        status = "Pending"
                        
                    frappe.db.set_value("FBR Queue", item.name, {
                        "status": status,
                        "retry_count": retry_count,
                        "last_retry_at": now(),
                        "error_message": result.get("error", "Unknown error")
                    })
                
            except Exception as e:
                # Mark as failed
                frappe.db.set_value("FBR Queue", item.name, {
                    "status": "Failed",
                    "error_message": str(e),
                    "retry_count": item.retry_count + 1
                })
                frappe.log_error(f"Error processing queue item {item.name}: {str(e)}", "FBR Queue Processing")
                
            frappe.db.commit()
        
        # Clean up old completed items (older than 30 days)
        cleanup_old_queue_items()
        
        return {"processed_count": processed_count}
        
    except Exception as e:
        frappe.log_error(f"Error processing FBR queue: {str(e)}", "FBR Queue")
        return {"processed_count": 0, "error": str(e)}

def process_queue_item(queue_item):
    """Process a single queue item"""
    try:
        from fbr_e_invoicing.api.fbr_submission import submit_single_invoice
        
        # Submit the invoice
        response = submit_single_invoice(
            queue_item.document_type, 
            queue_item.document_name, 
            is_retry=True
        )
        
        # Update the document with the response
        doc = frappe.get_doc(queue_item.document_type, queue_item.document_name)
        
        if queue_item.document_type == "Sales Invoice":
            doc.custom_fbr_response = json.dumps(response, indent=2)
            doc.custom_fbr_invoice_number = response.get("invoiceNumber", "")
            doc.custom_fbr_datetime = response.get("dated", "")
            doc.custom_fbr_status = response.get("validationResponse", {}).get("status", "")
        else:  # POS Invoice
            doc.custom_fbr_responce = json.dumps(response, indent=2)
            doc.custom_fbr_invoice_number = response.get("invoiceNumber", "")
            doc.custom_fbr_datetime = response.get("dated", "")
            doc.custom_fbr_status = response.get("validationResponse", {}).get("status", "")
        
        # doc.custom_fbr_queue_status = "Completed"
        doc.save(ignore_permissions=True)
        
        # Check if submission was successful
        status = response.get("validationResponse", {}).get("status", "")
        if status == "Valid":
            return {"success": True}
        else:
            return {"success": False, "error": f"FBR validation failed: {status}"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}

@frappe.whitelist()
def get_queue_status():
    """Get current queue status"""
    try:
        status_counts = frappe.db.sql("""
            SELECT 
                status,
                COUNT(*) as count
            FROM `tabFBR Queue`
            GROUP BY status
        """, as_dict=True)
        
        # Get failed items for review
        failed_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Failed"},
            fields=["document_type", "document_name", "error_message", "retry_count", "created_at"],
            limit=10
        )
        
        return {
            "status_counts": status_counts,
            "failed_items": failed_items
        }
        
    except Exception as e:
        frappe.log_error(f"Error getting queue status: {str(e)}", "FBR Queue")
        return {"status_counts": [], "failed_items": []}

@frappe.whitelist()
def retry_failed_items():
    """Retry all failed items in the queue"""
    try:
        # Reset failed items to pending (max 5 total retries)
        frappe.db.sql("""
            UPDATE `tabFBR Queue`
            SET status = 'Pending', error_message = ''
            WHERE status = 'Failed' AND retry_count < 5
        """)
        
        frappe.db.commit()
        
        # Get count of items that will be retried
        retry_count = frappe.db.count("FBR Queue", {"status": "Pending"})
        
        return {"retry_count": retry_count}
        
    except Exception as e:
        frappe.log_error(f"Error retrying failed items: {str(e)}", "FBR Queue")
        return {"retry_count": 0, "error": str(e)}

def cleanup_old_queue_items():
    """Clean up old completed queue items"""
    try:
        # Delete completed items older than 30 days
        cutoff_date = add_to_date(None, days=-30)
        
        frappe.db.sql("""
            DELETE FROM `tabFBR Queue`
            WHERE status = 'Completed' AND completed_at < %s
        """, cutoff_date)
        
        frappe.db.commit()
        
    except Exception as e:
        frappe.log_error(f"Error cleaning up queue: {str(e)}", "FBR Queue Cleanup")

# Scheduled task to process queue automatically
def process_fbr_queue_scheduled():
    """Scheduled task to process FBR queue"""
    try:
        # Only process if there are pending items
        pending_count = frappe.db.count("FBR Queue", {"status": "Pending"})
        
        if pending_count > 0:
            result = process_queue(limit=20)  # Process 20 items at a time
            frappe.log_error(f"Scheduled FBR queue processing: {result}", "FBR Queue Scheduled")
            
    except Exception as e:
        frappe.log_error(f"Error in scheduled FBR queue processing: {str(e)}", "FBR Queue Scheduled")