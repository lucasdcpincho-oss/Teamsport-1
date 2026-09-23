import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from db import db
from security import require_admin
from storage import put_object
from store_routes import promo_active

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

ORDER_STATUSES = ["recebida", "confirmada", "em_preparacao", "em_entrega", "entregue_paga", "cancelada"]

MIME_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4", "webm": "video/webm",
}


def compute_product_fields(p: dict) -> dict:
    original = float(p.get("price_original") or 0)
    sale = float(p.get("price_sale") or original)
    if original > 0 and sale < original:
        p["discount_percent"] = round((1 - sale / original) * 100)
        p["discount_euros"] = round(original - sale, 2)
    else:
        p["discount_percent"] = 0
        p["discount_euros"] = 0.0
    total_stock = 0
    available = set()
    for v in p.get("variants", []):
        for s, q in (v.get("sizes") or {}).items():
            q = int(q or 0)
            if q > 0:
                total_stock += q
                available.add(str(s))
    p["total_stock"] = total_stock
    p["available_sizes"] = sorted(available, key=lambda x: int(x) if str(x).isdigit() else 999)
    return p


# ---------- dashboard ----------

@router.get("/stats")
async def stats():
    orders = db.orders
    counts = {}
    for st in ORDER_STATUSES:
        counts[st] = await orders.count_documents({"status": st})

    async def sum_total(match):
        pipeline = [{"$match": match}, {"$group": {"_id": None, "t": {"$sum": "$total"}}}]
        agg = await orders.aggregate(pipeline).to_list(1)
        return round(agg[0]["t"], 2) if agg else 0.0

    value_sold = await sum_total({"status": {"$ne": "cancelada"}})
    money_received = await sum_total({"status": "entregue_paga"})

    low_stock = await db.products.find(
        {"active": True, "total_stock": {"$gt": 0, "$lte": 5}},
        {"_id": 0, "id": 1, "name": 1, "brand": 1, "total_stock": 1, "photos": 1, "variants": 1},
    ).sort("total_stock", 1).to_list(20)
    out_stock = await db.products.find(
        {"active": True, "total_stock": 0},
        {"_id": 0, "id": 1, "name": 1, "brand": 1, "total_stock": 1, "photos": 1},
    ).to_list(50)

    recent = await db.orders.find({}, {"_id": 0}).sort("created_at", -1).limit(8).to_list(8)
    active_promos = await db.products.count_documents({"active": True, "discount_percent": {"$gt": 0}})

    return {
        "orders": counts,
        "value_sold": value_sold,
        "money_received": money_received,
        "low_stock": low_stock,
        "out_of_stock": out_stock,
        "recent_orders": recent,
        "active_promos": active_promos,
        "total_products": await db.products.count_documents({"active": True}),
        "total_customers": len(await db.orders.distinct("customer.phone")),
    }


# ---------- produtos ----------

class VariantIn(BaseModel):
    color: str = ""
    sizes: dict[str, int] = {}


class ProductIn(BaseModel):
    name: str
    brand: str = ""
    model: str = ""
    category_id: str = ""
    short_description: str = ""
    description: str = ""
    sku: str = ""
    gender: str = "unissexo"
    usage_type: str = "casual"
    material: str = ""
    primary_color: str = ""
    secondary_colors: list[str] = []
    features: list[str] = []
    photos: list[str] = []
    videos: list[str] = []
    price_original: float = 0
    price_sale: float = 0
    promo_start: Optional[str] = None
    promo_end: Optional[str] = None
    variants: list[VariantIn] = []
    active: bool = True


@router.get("/products")
async def admin_list_products(search: Optional[str] = None, include_inactive: bool = True):
    query = {}
    if not include_inactive:
        query["active"] = True
    if search:
        rx = {"$regex": re.escape(search), "$options": "i"}
        query["$or"] = [{"name": rx}, {"brand": rx}, {"sku": rx}, {"model": rx}]
    items = await db.products.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for p in items:
        p["is_promo"] = promo_active(p)
    return items


@router.post("/products")
async def create_product(data: ProductIn):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="Nome obrigatÃ³rio")
    now = datetime.now(timezone.utc).isoformat()
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["sold_count"] = 0
    doc["created_at"] = now
    doc["updated_at"] = now
    if not doc["variants"]:
        doc["variants"] = [{"color": "", "sizes": {}}]
    doc = compute_product_fields(doc)
    await db.products.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/products/{product_id}")
async def update_product(product_id: str, data: ProductIn):
    existing = await db.products.find_one({"id": product_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Produto nÃ£o encontrado")
    doc = data.model_dump()
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not doc["variants"]:
        doc["variants"] = [{"color": "", "sizes": {}}]
    doc = compute_product_fields(doc)
    await db.products.update_one({"id": product_id}, {"$set": doc})
    updated = await db.products.find_one({"id": product_id}, {"_id": 0})
    return updated


@router.post("/products/{product_id}/duplicate")
async def duplicate_product(product_id: str):
    p = await db.products.find_one({"id": product_id}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Produto nÃ£o encontrado")
    now = datetime.now(timezone.utc).isoformat()
    p["id"] = str(uuid.uuid4())
    p["name"] = p["name"] + " (cÃ³pia)"
    p["sku"] = (p.get("sku") or "") + "-COPY"
    p["sold_count"] = 0
    p["active"] = False
    p["created_at"] = now
    p["updated_at"] = now
    await db.products.insert_one(p)
    p.pop("_id", None)
    return p


@router.patch("/products/{product_id}/active")
async def toggle_active(product_id: str):
    p = await db.products.find_one({"id": product_id}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Produto nÃ£o encontrado")
    new_val = not p.get("active", True)
    await db.products.update_one({"id": product_id}, {"$set": {"active": new_val, "updated_at": datetime.now(timezone.utc).isoformat()}})
    return {"id": product_id, "active": new_val}


@router.delete("/products/{product_id}")
async def delete_product(product_id: str):
    res = await db.products.delete_one({"id": product_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Produto nÃ£o encontrado")
    return {"ok": True}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = (file.filename or "bin").split(".")[-1].lower()
    content_type = file.content_type or MIME_TYPES.get(ext, "application/octet-stream")
    if not (content_type.startswith("image/") or content_type.startswith("video/")):
        raise HTTPException(status_code=400, detail="Apenas imagens ou vÃ­deos")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ficheiro demasiado grande (mÃ¡x. 50MB)")
    path = f"teamsport/uploads/{uuid.uuid4()}.{ext}"
    result = put_object(path, data, content_type)
    await db.files.insert_one({
        "id": str(uuid.uuid4()),
        "storage_path": result["path"],
        "original_filename": file.filename,
        "content_type": content_type,
        "size": result.get("size", len(data)),
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"path": result["path"], "url": f"/api/files/{result['path']}"}


# ---------- categorias ----------

class CategoryIn(BaseModel):
    name_pt: str
    name_en: str = ""
    order: int = 0
    active: bool = True


@router.get("/categories")
async def admin_list_categories():
    return await db.categories.find({}, {"_id": 0}).sort("order", 1).to_list(500)


@router.post("/categories")
async def create_category(data: CategoryIn):
    if not data.name_pt.strip():
        raise HTTPException(status_code=400, detail="Nome obrigatÃ³rio")
    slug = re.sub(r"[^a-z0-9]+", "-", data.name_pt.lower()).strip("-")
    doc = {
        "id": str(uuid.uuid4()),
        "name_pt": data.name_pt.strip(),
        "name_en": data.name_en.strip() or data.name_pt.strip(),
        "slug": slug,
        "order": data.order,
        "active": data.active,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.categories.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/categories/reorder")
async def reorder_categories(order: list[str]):
    for i, cid in enumerate(order):
        await db.categories.update_one({"id": cid}, {"$set": {"order": i}})
    return {"ok": True}


@router.put("/categories/{cat_id}")
async def update_category(cat_id: str, data: CategoryIn):
    res = await db.categories.update_one(
        {"id": cat_id},
        {"$set": {"name_pt": data.name_pt, "name_en": data.name_en or data.name_pt, "order": data.order, "active": data.active}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Categoria nÃ£o encontrada")
    return await db.categories.find_one({"id": cat_id}, {"_id": 0})


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str):
    res = await db.categories.delete_one({"id": cat_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Categoria nÃ£o encontrada")
    return {"ok": True}


# ---------- encomendas ----------

@router.get("/orders")
async def admin_list_orders(status: Optional[str] = None):
    query = {}
    if status:
        query["status"] = status
    return await db.orders.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)


@router.patch("/orders/{order_id}/status")
async def update_order_status(order_id: str, payload: dict):
    status = payload.get("status")
    if status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Estado invÃ¡lido")
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Encomenda nÃ£o encontrada")
    now = datetime.now(timezone.utc).isoformat()
    old_status = order.get("status")
    update = {
        "status": status,
        "updated_at": now,
        "paid": status == "entregue_paga",
    }
    history = order.get("status_history", []) + [{"status": status, "at": now}]
    update["status_history"] = history
    await db.orders.update_one({"id": order_id}, {"$set": update})

    # repÃµe o stock ao cancelar
    if status == "cancelada" and old_status != "cancelada":
        for item in order.get("items", []):
            p = await db.products.find_one({"id": item["product_id"]}, {"_id": 0})
            if not p:
                continue
            variants = p.get("variants", [])
            vi = next((i for i, v in enumerate(variants) if v.get("color", "") == item.get("color", "")), 0)
            if variants:
                await db.products.update_one(
                    {"id": item["product_id"]},
                    {"$inc": {f"variants.{vi}.sizes.{item['size']}": item["qty"], "sold_count": -item["qty"]}},
                )
                p2 = await db.products.find_one({"id": item["product_id"]}, {"_id": 0})
                total_stock = 0
                available = set()
                for v in p2.get("variants", []):
                    for s, q in (v.get("sizes") or {}).items():
                        if q and q > 0:
                            total_stock += q
                            available.add(str(s))
                await db.products.update_one(
                    {"id": item["product_id"]},
                    {"$set": {"total_stock": total_stock, "available_sizes": sorted(available)}},
                )
    return await db.orders.find_one({"id": order_id}, {"_id": 0})


# ---------- clientes ----------

@router.get("/customers")
async def admin_list_customers():
    pipeline = [
        {"$group": {
            "_id": {"$ifNull": ["$customer.email", "$customer.phone"]},
            "name": {"$last": "$customer.name"},
            "phone": {"$last": "$customer.phone"},
            "email": {"$last": "$customer.email"},
            "city": {"$last": "$customer.city"},
            "orders_count": {"$sum": 1},
            "total_value": {"$sum": {"$cond": [{"$eq": ["$status", "cancelada"]}, 0, "$total"]}},
            "last_order": {"$max": "$created_at"},
        }},
        {"$sort": {"last_order": -1}},
    ]
    agg = await db.orders.aggregate(pipeline).to_list(1000)
    customers = []
    for c in agg:
        customers.append({
            "key": c["_id"] or c.get("phone", ""),
            "name": c.get("name", ""),
            "phone": c.get("phone", ""),
            "email": c.get("email", ""),
            "city": c.get("city", ""),
            "orders_count": c["orders_count"],
            "total_value": round(c["total_value"], 2),
            "last_order": c["last_order"],
        })
    registered = await db.users.find({"role": "customer"}, {"_id": 0, "password_hash": 0}).to_list(1000)
    known = {c["email"] for c in customers if c["email"]}
    for u in registered:
        if u["email"] not in known:
            customers.append({
                "key": u["email"], "name": u.get("name", ""), "phone": u.get("phone", ""),
                "email": u["email"], "city": "", "orders_count": 0, "total_value": 0,
                "last_order": u.get("created_at", ""), "registered": True,
            })
    return customers


@router.get("/customers/{key}/orders")
async def admin_customer_orders(key: str):
    orders = await db.orders.find(
        {"$or": [{"customer.email": key}, {"customer.phone": key}]},
        {"_id": 0},
    ).sort("created_at", -1).to_list(200)
    return orders
