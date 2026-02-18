# 🧾 FBR E-Invoicing for ERPNext

A comprehensive **Pakistan Federal Board of Revenue (FBR)** electronic invoicing integration for ERPNext with advanced features including intelligent queuing, automated retry mechanisms, bulk operations, and real-time monitoring.

## 🌟 Key Features

### ✨ **Smart Automation**
- **Hidden Checkbox**: "Submit to FBR" field (default: checked) controls submission
- **Intelligent Validation**: Real-time validation of FBR requirements
- **Date Validation**: Ensures posting date is current date for FBR compliance

### 🔄 **Advanced Queue Management**
- **Automatic Queuing**: Failed submissions automatically enter retry queue
- **Priority Processing**: High-value invoices get priority treatment  
- **Exponential Backoff**: Smart retry mechanism with increasing delays
- **Maximum Retries**: Configurable retry limits (default: 5 attempts)
- **Stuck Item Recovery**: Auto-detection and recovery of stuck processing items

### 📊 **Bulk Operations**
- **Bulk Submission**: Select multiple invoices for batch FBR submission
- **Progress Tracking**: Real-time progress indicators for bulk operations
- **Batch Processing**: Optimized processing of large invoice volumes
- **Queue Monitoring**: Live status updates during processing

### 🎛️ **Comprehensive Dashboard**
- **Real-time Statistics**: Today's submissions, success rates, error counts
- **Interactive Controls**: One-click queue processing and retry operations
- **Visual Indicators**: Color-coded status displays and progress bars
- **Recent Activity**: Timeline of latest FBR interactions

### 📋 **Detailed Logging**
- **Complete Audit Trail**: Every FBR interaction logged with full details
- **Request/Response Storage**: Full payload and response data preservation
- **Error Analysis**: Detailed error messages and troubleshooting information
- **Performance Metrics**: Processing times and performance analytics

## 🚀 Installation Guide

### Step 1: Install the App

```bash
# Get the app
bench get-app https://github.com/your-repo/fbr_e_invoicing.git

# Install on your site
bench --site your-site install-app fbr_e_invoicing

# Migrate the database
bench --site your-site migrate
```

### Step 2: Post-Installation Setup

The app automatically sets up:
- ✅ Pakistani provinces (Punjab, Sindh, KPK, Balochistan, etc.)
- ✅ Sample HS codes for common items
- ✅ Custom fields on Sales Invoice and POS Invoice
- ✅ FBR workspace with shortcuts
- ✅ Default permissions and roles
- ✅ Print formats for FBR invoices

### Step 3: Configuration

1. **Navigate to FBR E-Inv Setup**
   ```
   Setup → FBR E-Invoicing → FBR E-Inv Setup
   ```

2. **Configure API Settings**:
   - **API Endpoint**: `https://api.fbr.gov.pk/einvoicing`
   - **PRAL Authorization Token**: Your FBR API token
   - **PRAL Login ID**: Your FBR login ID  
   - **PRAL Login Password**: Your FBR password

3. **Verify Setup**:
   - Test API connectivity
   - Validate credentials
   - Check province and HS code data

## 📖 User Manual

### For Sales Invoices

#### Creating and Submitting Invoices

1. **Create Invoice** (Standard Process):
   ```
   Sales → Sales Invoice → New
   ```
   - Fill standard invoice details
   - System validates FBR requirements automatically
   - Hidden "Submit to FBR" checkbox is checked by default

2. **Manual FBR Submission**:
   - Click **"Post to FBR"** button (for draft invoices)
   - System validates posting date (must be current date)
   - Real-time submission with progress indicator
   - Response saved in custom fields

3. **Bulk FBR Submission**:
   ```
   Sales → Sales Invoice → List View
   ```
   - Select multiple invoices using checkboxes
   - Click **"Bulk Submit to FBR"** action button
   - Confirm submission for selected invoices
   - All items queued for processing

4. **Monitor Status**:
   - **FBR Status**: Valid/Invalid/Error indicator
   - **FBR Invoice Number**: Unique FBR reference
   - **FBR DateTime**: Submission timestamp
   - **Queue Status**: Processing progress

#### Handling Failed Submissions

1. **Automatic Queue**: Failed submissions auto-added to retry queue
2. **Manual Retry**: Click **"Retry FBR"** button on failed invoices  
3. **Bulk Retry**: Use list view actions for multiple failed invoices
4. **Error Analysis**: Review error messages in FBR response fields

### For POS Invoices

#### Automatic Submission Workflow

1. **Create POS Invoice** (Standard Process):
   ```
   Retail → POS Invoice → New
   ```
   - Complete invoice normally
   - System builds FBR payload automatically

2. **Auto-Submit on Save**:
   - Invoice automatically submits to FBR when saved
   - **Valid Status**: Transaction complete
   - **Invalid Status**: Added to retry queue automatically
   - **Error Status**: Added to retry queue with error details

3. **Queue Processing**:
   - Invalid/error submissions queued automatically
   - Scheduled job processes queue every 15 minutes
   - Manual processing available via dashboard

#### Manual Operations

1. **Manual Submission**: Click **"Post to FBR"** button
2. **Retry Failed**: Click **"Retry FBR"** for failed submissions
3. **View Payload**: Click **"Show FBR Payload"** to see JSON data
4. **Bulk Operations**: Available in list view

### Queue Management

#### Accessing Queue
```
FBR E-Invoicing → FBR Queue
```

#### Queue Statuses
- 🟡 **Pending**: Waiting for processing
- 🔵 **Processing**: Currently being submitted  
- 🟢 **Completed**: Successfully submitted
- 🔴 **Failed**: Exceeded maximum retries

#### Queue Operations

1. **Manual Processing**:
   ```
   FBR Dashboard → Process Queue
   ```
   - Processes up to 50 pending items
   - Real-time progress tracking
   - Results displayed immediately

2. **Retry Failed Items**:
   ```
   FBR Dashboard → Retry Failed
   ```
   - Resets failed items to pending status
   - Respects maximum retry limits
   - Batch retry for efficiency

3. **Reset Stuck Items**:
   - Automatically detects items stuck in "Processing" 
   - Resets to "Pending" after 1 hour
   - Available via API or dashboard

### Monitoring and Analytics

#### FBR Dashboard
Access: `/fbr-dashboard` or **FBR E-Invoicing Workspace**

**Features**:
- 📊 Real-time submission statistics
- 🎯 Success rate tracking  
- 📈 Queue processing status
- 🔄 One-click management actions
- 📝 Recent activity timeline

#### FBR Logs
```
FBR E-Invoicing → FBR Logs
```

**Contains**:
- Complete request/response data
- Processing time metrics
- Error details and analysis
- API status codes and responses
- User and IP tracking

#### Compliance Reporting

1. **Generate Reports**:
   ```python
   # Via API
   frappe.call('fbr_e_invoicing.api.fbr_validation.get_fbr_compliance_report', {
       from_date: '2024-01-01',
       to_date: '2024-01-31'
   })
   ```

2. **Report Contents**:
   - Total invoices eligible for FBR
   - Successfully submitted count
   - Compliance rate percentage
   - Failed submissions analysis
   - Customer-wise breakdown

## 🔧 Advanced Configuration

### Custom Validation Rules

Add business-specific validation in `hooks.py`:

```python
doc_events = {
    "Sales Invoice": {
        "validate": [
            "fbr_e_invoicing.api.fbr_validation.validate_fbr_fields",
            "your_app.custom_fbr_validation.validate_high_value_invoices"
        ]
    }
}
```

### Queue Priority Configuration

Customize priority based on business rules:

```python
def get_invoice_priority(doc):
    """Custom priority logic"""
    if doc.grand_total > 100000:
        return 1  # High priority
    elif doc.customer_group == "VIP":
        return 2  # Medium-high priority
    return 5  # Normal priority
```

### Scheduled Job Configuration

Modify processing frequency in `hooks.py`:

```python
scheduler_events = {
    "cron": {
        "*/10 * * * *": [  # Every 10 minutes
            "fbr_e_invoicing.api.fbr_queue.process_fbr_queue_scheduled"
        ],
        "0 */2 * * *": [  # Every 2 hours
            "fbr_e_invoicing.api.fbr_maintenance.reset_stuck_queue_items"
        ]
    }
}
```

### Notification Customization

Set up custom alerts:

```python
def send_custom_fbr_alert(doc):
    if doc.grand_total > 500000:  # High-value transactions
        # Send SMS/WhatsApp notification
        send_high_value_alert(doc)
```

## 🛠️ API Reference

### Core Submission APIs

#### Submit Single Invoice
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_submission.submit_single_invoice',
    args: {
        doctype: 'Sales Invoice',
        docname: 'SI-2024-001',
        is_retry: false
    }
})
```

#### Bulk Submit Invoices
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_submission.bulk_submit_invoices',
    args: {
        doctype: 'Sales Invoice', 
        docnames: ['SI-2024-001', 'SI-2024-002']
    }
})
```

### Queue Management APIs

#### Process Queue
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_queue.process_queue',
    args: {
        limit: 50  // Maximum items to process
    }
})
```

#### Add to Queue
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_queue.add_to_queue',
    args: {
        doctype: 'POS Invoice',
        docname: 'POS-2024-001', 
        status: 'Pending',
        priority: 5
    }
})
```

### Validation APIs

#### Validate Document
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_validation.validate_fbr_document',
    args: {
        doctype: 'Sales Invoice',
        docname: 'SI-2024-001'
    }
})
```

#### Check API Health
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_validation.check_fbr_api_status'
})
```

### Maintenance APIs

#### Generate Health Report
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_maintenance.generate_fbr_health_report'
})
```

#### Cleanup Old Records
```python
frappe.call({
    method: 'fbr_e_invoicing.api.fbr_maintenance.cleanup_old_records'
})
```

## 🔍 Troubleshooting

### Common Issues

#### 1. Validation Errors
```
Error: Buyer Province is required for FBR submission
```
**Solution**: 
- Ensure `custom_province` field is set on Sales Invoice
- Verify customer has province configured
- Check company province settings

#### 2. API Connection Issues
```
Error: FBR API request failed - Connection timeout
```
**Solution**:
- Verify internet connectivity
- Check FBR API endpoint in settings
- Validate authorization token
- Review firewall/proxy settings

#### 3. Queue Processing Problems
```
Error: Queue items stuck in Processing
```
**Solution**:
- Use reset stuck items function
- Check for crashed processes
- Review system resources
- Restart scheduled jobs if needed

#### 4. Missing HS Codes
```
Error: HS Code required for FBR submission
```
**Solution**:
- Add HS codes to item master
- Set default HS codes for item groups
- Use bulk update tools for existing items

### Debug Mode

Enable detailed logging:

```json
// site_config.json
{
    "developer_mode": 1,
    "log_level": "DEBUG",
    "fbr_debug_mode": 1
}
```

View logs:
```bash
# ERPNext logs
tail -f sites/your-site/logs/worker.log

# FBR specific logs
grep "FBR" sites/your-site/logs/worker.log
```

### Performance Optimization

#### Database Optimization
```sql
-- Add indexes for better performance
CREATE INDEX idx_sales_invoice_fbr ON `tabSales Invoice` (custom_fbr_status, posting_date);
CREATE INDEX idx_fbr_queue_status ON `tabFBR Queue` (status, priority, created_at);
```

#### Queue Processing Optimization
```python
# Adjust batch size based on server capacity
def process_fbr_queue_optimized():
    system_load = get_system_load()
    batch_size = 20 if system_load < 50 else 10
    return process_queue(limit=batch_size)
```

## 📊 Reporting

### Built-in Reports

1. **FBR Compliance Summary**
   - Period-wise submission statistics  
   - Success/failure rates
   - Non-compliant invoices list

2. **FBR Submission Analytics**
   - Daily/weekly/monthly trends
   - Customer-wise submission data
   - Error pattern analysis

3. **Queue Performance Report**
   - Processing time metrics
   - Retry statistics
   - System performance indicators

### Custom Reports

Create custom reports using **Report Builder**:

```sql
-- Sample: Daily FBR Summary
SELECT 
    DATE(posting_date) as date,
    COUNT(*) as total_invoices,
    COUNT(CASE WHEN custom_fbr_status = 'Valid' THEN 1 END) as successful,
    COUNT(CASE WHEN custom_fbr_status = 'Invalid' THEN 1 END) as invalid,
    COUNT(CASE WHEN custom_fbr_status IS NULL THEN 1 END) as pending
FROM `tabSales Invoice`
WHERE custom_submit_to_fbr = 1
GROUP BY DATE(posting_date)
ORDER BY date DESC
```

## 🔐 Security & Compliance

### Data Protection
- ✅ Encrypted API credentials storage
- ✅ User access controls and permissions
- ✅ Audit trail for all FBR operations
- ✅ Data retention policies (90 days for logs)
- ✅ Personal data redaction in exports

### Access Control
- **System Manager**: Full access to all FBR features
- **Accounts Manager**: Submit, retry, view all FBR data
- **Accounts User**: View FBR status, limited queue access
- **FBR Manager**: Custom role for FBR-specific operations

### Compliance Features
- Complete transaction logging
- Immutable audit trail
- Data integrity verification
- Regular compliance reporting
- Automated backup inclusion

## 🚦 Production Deployment

### Pre-Production Checklist

- [ ] FBR API credentials configured
- [ ] All items have HS codes assigned
- [ ] Customer/company provinces set
- [ ] Print formats customized
- [ ] User training completed
- [ ] Backup procedures verified

### Go-Live Process

1. **Data Migration**:
   ```bash
   bench --site your-site execute fbr_e_invoicing.patches.v1_0.migrate_existing_data.execute
   ```

2. **User Training**:
   - Conduct training sessions
   - Provide user manuals
   - Set up help desk procedures

3. **Monitoring Setup**:
   - Configure alerts for failed submissions
   - Set up daily summary reports
   - Monitor queue performance

4. **Gradual Rollout**:
   - Start with test invoices
   - Enable for specific users/branches
   - Full rollout after validation

## 📞 Support

### Getting Help

1. **Documentation**: Check this README and inline help
2. **Error Logs**: Review **Error Log** doctype for issues  
3. **FBR Logs**: Check detailed API responses
4. **Community**: ERPNext community forums
5. **Professional**: Contact certified ERPNext partners

### Reporting Issues

When reporting issues, include:
- ERPNext version
- App version
- Error logs
- Steps to reproduce
- System configuration

### Contributing

1. Fork the repository
2. Create feature branch
3. Make changes with tests
4. Submit pull request
5. Follow coding standards

## 📅 Roadmap

### Version 2.0 (Planned)
- [ ] Multiple FBR API provider support
- [ ] Advanced analytics dashboard  
- [ ] Mobile app integration
- [ ] Automated reconciliation with FBR
- [ ] Multi-language support (Urdu)

### Version 2.1 (Future)
- [ ] AI-powered error resolution
- [ ] Blockchain integration
- [ ] Advanced reporting suite
- [ ] API rate limiting management
- [ ] Multi-tenant optimizations

---

## 📄 License

MIT License - see [LICENSE](license.txt) file for details.

---

**🎉 Congratulations!** 

You now have a comprehensive, enterprise-grade FBR E-Invoicing solution that handles everything from basic submissions to advanced queue management, monitoring, and compliance reporting. 

**Happy Invoicing!** 📋✨