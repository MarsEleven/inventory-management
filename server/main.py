from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

# Per-warehouse delivery lead time for restocking orders.
# Values reflect approximate shipping distance from a (hypothetical) US-based
# central supplier; used to compute expected_delivery on submitted orders.
WAREHOUSE_LEAD_TIME_DAYS = {
    'San Francisco': 5,
    'London': 10,
    'Tokyo': 14,
}
DEFAULT_LEAD_TIME_DAYS = 10

# Synthetic unit-cost fallback for forecast SKUs that aren't present in
# inventory.json (the demo data only overlaps on 1 of 9 forecast SKUs).
# A production system would have a real SKU master.
SKU_PREFIX_UNIT_COST = {
    'WDG': 15.00,
    'BRG': 8.50,
    'GSK': 2.25,
    'MTR': 120.00,
    'FLT': 12.75,
    'VLV': 45.00,
    'PSU': 18.99,
    'SNR': 25.00,
    'CTL': 75.00,
}
DEFAULT_UNIT_COST = 20.00

# In-memory store for restocking orders submitted via /api/restocking/orders.
# Not persisted — restart clears these (matches mock_data pattern).
submitted_orders: List[dict] = []
_submitted_order_seq = 0  # incrementing id source

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    notes: Optional[str] = None

class RestockingRecommendation(BaseModel):
    item_sku: str
    item_name: str
    warehouse: str
    current_demand: int
    forecasted_demand: int
    suggested_quantity: int  # = forecasted_demand - current_demand
    unit_cost: float
    total_cost: float
    lead_time_days: int

class RestockingOrderItem(BaseModel):
    item_sku: str
    item_name: str
    warehouse: str
    quantity: int
    unit_cost: float
    total_cost: float

class PlaceRestockingOrderRequest(BaseModel):
    items: List[RestockingOrderItem]
    budget: Optional[float] = None  # informational; carried into the record

class SubmittedOrder(BaseModel):
    id: str
    order_number: str
    items: List[RestockingOrderItem]
    total_value: float
    submitted_at: str
    expected_delivery: str
    lead_time_days: int  # max lead time across the order's warehouses
    warehouses: List[str]
    status: str  # always "Submitted" on creation
    budget: Optional[float] = None

# --- Restocking helpers --------------------------------------------------

def _resolve_sku_metadata(sku: str) -> dict:
    """Return {unit_cost, warehouse} for a forecast SKU.

    Prefers a real inventory match. Falls back to a SKU-prefix unit cost and
    a deterministic warehouse assignment when the demo data has no match.
    """
    match = next((i for i in inventory_items if i['sku'] == sku), None)
    if match:
        return {'unit_cost': match['unit_cost'], 'warehouse': match['warehouse']}

    prefix = sku.split('-')[0] if '-' in sku else sku
    unit_cost = SKU_PREFIX_UNIT_COST.get(prefix, DEFAULT_UNIT_COST)
    # Deterministic warehouse round-robin so the same SKU always lands in the
    # same warehouse across requests (stable lead times in the UI).
    warehouses = list(WAREHOUSE_LEAD_TIME_DAYS.keys())
    warehouse = warehouses[hash(sku) % len(warehouses)]
    return {'unit_cost': unit_cost, 'warehouse': warehouse}

def _lead_time(warehouse: str) -> int:
    return WAREHOUSE_LEAD_TIME_DAYS.get(warehouse, DEFAULT_LEAD_TIME_DAYS)

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

# --- Restocking endpoints ------------------------------------------------

@app.get("/api/restocking/recommendations", response_model=List[RestockingRecommendation])
def get_restocking_recommendations(budget: float = 0):
    """Greedy budget-fit restocking recommendations.

    Algorithm: rank demand-forecast items by demand gap (forecasted minus
    current) descending, then walk the list adding items whose total_cost
    fits in the remaining budget. Items that don't fit are skipped so smaller
    items further down the list still have a chance.
    """
    if budget < 0:
        raise HTTPException(status_code=400, detail="Budget must be non-negative")

    candidates = []
    for f in demand_forecasts:
        gap = f['forecasted_demand'] - f['current_demand']
        if gap <= 0:
            continue  # demand falling or steady — no need to restock
        meta = _resolve_sku_metadata(f['item_sku'])
        total = round(gap * meta['unit_cost'], 2)
        candidates.append({
            'item_sku': f['item_sku'],
            'item_name': f['item_name'],
            'warehouse': meta['warehouse'],
            'current_demand': f['current_demand'],
            'forecasted_demand': f['forecasted_demand'],
            'suggested_quantity': gap,
            'unit_cost': meta['unit_cost'],
            'total_cost': total,
            'lead_time_days': _lead_time(meta['warehouse']),
            '_gap': gap,
        })

    # Largest gap first; tie-break by lower total_cost so smaller items fit
    # when gaps are equal.
    candidates.sort(key=lambda c: (-c['_gap'], c['total_cost']))

    selected = []
    remaining = budget
    for c in candidates:
        if c['total_cost'] <= remaining:
            selected.append({k: v for k, v in c.items() if not k.startswith('_')})
            remaining -= c['total_cost']

    return selected

@app.post("/api/restocking/orders", response_model=SubmittedOrder)
def place_restocking_order(req: PlaceRestockingOrderRequest):
    """Submit a restocking order and record it in the in-memory store."""
    if not req.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    global _submitted_order_seq
    _submitted_order_seq += 1
    now = datetime.now()

    # Multi-warehouse orders take the slowest warehouse's lead time so the
    # promised delivery date isn't optimistic.
    warehouses = sorted({item.warehouse for item in req.items})
    lead = max((_lead_time(w) for w in warehouses), default=DEFAULT_LEAD_TIME_DAYS)
    expected = (now + timedelta(days=lead)).date().isoformat()
    total = round(sum(item.total_cost for item in req.items), 2)

    order = {
        'id': f"sub-{_submitted_order_seq}",
        'order_number': f"RST-{now.strftime('%Y%m%d')}-{_submitted_order_seq:04d}",
        'items': [item.dict() for item in req.items],
        'total_value': total,
        'submitted_at': now.isoformat(timespec='seconds'),
        'expected_delivery': expected,
        'lead_time_days': lead,
        'warehouses': warehouses,
        'status': 'Submitted',
        'budget': req.budget,
    }
    submitted_orders.append(order)
    return order

@app.get("/api/submitted-orders", response_model=List[SubmittedOrder])
def get_submitted_orders():
    """List restocking orders submitted via /api/restocking/orders.

    Path is /api/submitted-orders (not /api/orders/submitted) to avoid being
    shadowed by the /api/orders/{order_id} route declared above.
    """
    # Newest first so the most recent submission appears at the top.
    return sorted(submitted_orders, key=lambda o: o['submitted_at'], reverse=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
