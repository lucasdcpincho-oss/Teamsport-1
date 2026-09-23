import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from db import db
from security import get_optional_user
from storage import get_object

router = APIRouter(prefix="/api", tags=["store"])

GENDERS = ["masculino", "feminino", "unissexo", "crianca"]
USAGE_TYPES = ["running", "futebol", "basquetebol", "casual", "treino", "outro"]
DEFAULT_SIZES = [str(s) for s in range(36, 47)]

INITIAL_CATEGORIES = [
    {"name_pt": "Sapatilhas", "name_en": "Sneakers", "slug": "sapatilhas"},
    {"name_pt": "Running", "name_en": "Running", "slug": "running"},
    {"name_pt": "Futebol", "name_en": "Football", "slug": "futebol"},
    {"name_pt": "Basquetebol", "name_en": "Basketball", "slug": "basquetebol"},
    {"name_pt": "Treino", "name_en": "Training", "slug": "treino"},
    {"name_pt": "Casual", "name_en": "Casual", "slug": "casual"},
    {"name_pt": "CrianÃ§a", "name_en": "Kids", "slug": "crianca"},
]


async def seed_categories():
    count = await db.categories.count_documents({})
    if count > 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    for i, cat in enumerate(INITIAL_CATEGORIES):
        await db.categories.insert_one({
            "id": str(uuid.uuid4()),
            **cat,
            "order": i,
            "active": True,
            "created_at": now,
        })


def promo_active(p: dict) -> bool:
    if not p.get("discount_percent"):
        return False
    now = datetime.now(timezone.utc).date()
    start = p.get("promo_start")
    end = p.get("promo_end")
    if start:
        try:
            if now < datetime.fromisoformat(str(start)[:10]).date():
                return False
        except ValueError:
            pass
    if end:
        try:
            if now > datetime.fromisoformat(str(end)[:10]).date():
                return False
        except ValueError:
            pass
    return True


def decorate_product(p: dict) -> dict:
    p["is_promo"] = promo_active(p)
    p["total_stock"] = p.get("total_stock", 0)
    return p


@router.get("/categories")
async def list_categories():
    cats = await db.categories.find({"active": True}, {"_id": 0}).sort("order", 1).to_list(200)
    return cats


@router.get("/products/meta")
async def products_meta():
    brands = await db.products.distinct("brand", {"active": True})
    colors = await db.products.distinct("primary_color", {"active": True})
    sizes = await db.products.distinct("available_sizes", {"active": True})
    pipeline = [
        {"$match": {"active": True}},
        {"$group": {"_id": None, "min": {"$min": "$price_sale"}, "max": {"$max": "$price_sale"}}},
    ]
    agg = await db.products.aggregate(pipeline).to_list(1)
    price = agg[0] if agg else {"min": 0, "max": 0}
    def num(x):
        try:
            return int(str(x))
        except (TypeError, ValueError):
            return None
    size_list = sorted({s for s in (num(x) for x in sizes) if s is not None})
    return {
        "brands": sorted([b for b in brands if b]),
        "colors": sorted([c for c in colors if c]),
        "sizes": size_list,
        "price_min": price.get("min") or 0,
        "price_max": price.get("max") or 0,
        "genders": GENDERS,
        "usage_types": USAGE_TYPES,
    }


@router.get("/products")
async def list_products(
    request: Request,
    category: Optional[str] = None,
    brand: Optional[str] = None,
    gender: Optional[str] = None,
    usage_type: Optional[str] = None,
    color: Optional[str] = None,
    size: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    promo: Optional[bool] = None,
    in_stock: Optional[bool] = None,
    search: Optional[str] = None,
    sort: str = "newest",
    page: int = 1,
    limit: int = 12,
):
    query = {"active": True}
    if category:
        query["category_id"] = category
    if brand:
        query["brand"] = {"$in": [b.strip() for b in brand.split(",") if b.strip()]}
    if gender:
        query["gender"] = gender
    if usage_type:
        query["usage_type"] = usage_type
    and_conditions = []
    if color:
        rx = {"$regex": re.escape(color), "$options": "i"}
        and_conditions.append({"$or": [{"primary_color": rx}, {"secondary_colors": rx}, {"variants.color": rx}]})
    if search:
        rx = {"$regex": re.escape(search), "$options": "i"}
        and_conditions.append({"$or": [{"name": rx}, {"brand": rx}, {"model": rx}, {"sku": rx}]})
    if and_conditions:
        query["$and"] = and_conditions
    if size:
        query["available_sizes"] = size
    if in_stock:
        query["total_stock"] = {"$gt": 0}
    if min_price is not None or max_price is not None:
        pr = {}
        if min_price is not None:
            pr["$gte"] = min_price
        if max_price is not None:
            pr["$lte"] = max_price
        query["price_sale"] = pr
    if promo:
        query["discount_percent"] = {"$gt": 0}

    sort_map = {
        "newest": ("created_at", -1),
        "best_sellers": ("sold_count", -1),
        "price_asc": ("price_sale", 1),
        "price_desc": ("price_sale", -1),
        "discount": ("discount_percent", -1),
    }
    sort_field, sort_dir = sort_map.get(sort, sort_map["newest"])

    page = max(page, 1)
    limit = min(max(limit, 1), 60)
    total = await db.products.count_documents(query)
    items = await db.products.find(query, {"_id": 0}).sort(sort_field, sort_dir).skip((page - 1) * limit).limit(limit).to_list(limit)
    items = [decorate_product(p) for p in items]
    if promo:
        items = [p for p in items if p["is_promo"]]
    return {"items": items, "total": total, "page": page, "pages": max(1, -(-total // limit))}


@router.get("/products/{product_id}")
async def get_product(product_id: str):
    p = await db.products.find_one({"id": product_id, "active": True}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Produto nÃ£o encontrado")
    return decorate_product(p)


class OrderItemIn(BaseModel):
    product_id: str
    color: str = ""
    size: str
    qty: int = 1


class CustomerIn(BaseModel):
    name: str
    phone: str
    email: str = ""
    address: str
    postal_code: str
    city: str
    notes: str = ""


class OrderIn(BaseModel):
    customer: CustomerIn
    items: list[OrderItemIn]


@router.post("/orders")
async def create_order(data: OrderIn, request: Request):
    if not data.items:
        raise HTTPException(status_code=400, detail="O carrinho estÃ¡ vazio")
    if not data.customer.name.strip() or not data.customer.phone.strip():
        raise HTTPException(status_code=400, detail="Nome e telefone sÃ£o obrigatÃ³rios")

    user = await get_optional_user(request)
    order_items = []
    subtotal = 0.0
    updates = []

    for item in data.items:
        if item.qty < 1:
            raise HTTPException(status_code=400, detail="Quantidade invÃ¡lida")
        p = await db.products.find_one({"id": item.product_id, "active": True}, {"_id": 0})
        if not p:
            raise HTTPException(status_code=400, detail=f"Produto indisponÃ­vel: {item.product_id}")
        variants = p.get("variants") or []
        vi = 0
        color_variants = [v for v in variants if v.get("color")]
        if item.color:
            vi = next((i for i, v in enumerate(variants) if v.get("color", "") == item.color), -1)
            if vi == -1:
                raise HTTPException(status_code=400, detail=f"Cor indisponÃ­vel para {p.get('name')}")
        elif len(color_variants) > 1:
            raise HTTPException(status_code=400, detail=f"Seleciona a cor para {p.get('name')}")
        if not variants:
            raise HTTPException(status_code=400, detail=f"Produto sem stock configurado: {p.get('name')}")
        sizes = variants[vi].get("sizes", {})
        stock = sizes.get(item.size)
        if stock is None or stock < item.qty:
            raise HTTPException(status_code=400, detail=f"Tamanho {item.size} esgotado para {p.get('name')}")
        unit_price = float(p.get("price_sale") or p.get("price_original") or 0)
        line_total = round(unit_price * item.qty, 2)
        subtotal += line_total
        photos = p.get("photos") or []
        order_items.append({
            "product_id": p["id"],
            "name": p.get("name", ""),
            "brand": p.get("brand", ""),
            "model": p.get("model", ""),
            "sku": p.get("sku", ""),
            "photo": photos[0] if photos else "",
            "color": item.color or (variants[vi].get("color") or ""),
            "size": item.size,
            "qty": item.qty,
            "unit_price": unit_price,
            "line_total": line_total,
        })
        updates.append((p["id"], vi, item.size, item.qty))

    for pid, vi, size, qty in updates:
        res = await db.products.update_one(
            {"id": pid, f"variants.{vi}.sizes.{size}": {"$gte": qty}},
            {"$inc": {f"variants.{vi}.sizes.{size}": -qty, "sold_count": qty}},
        )
        if res.matched_count == 0:
            raise HTTPException(status_code=409, detail="Stock insuficiente â€” atualiza o carrinho")
        p = await db.products.find_one({"id": pid}, {"_id": 0})
        total_stock = 0
        available = set()
        for v in p.get("variants", []):
            for s, q in (v.get("sizes") or {}).items():
                if q and q > 0:
                    total_stock += q
                    available.add(str(s))
        await db.products.update_one(
            {"id": pid},
            {"$set": {"total_stock": total_stock, "available_sizes": sorted(available, key=lambda x: int(x) if x.isdigit() else 0)}},
        )

    now = datetime.now(timezone.utc).isoformat()
    order = {
        "id": str(uuid.uuid4()),
        "order_code": "TS-" + uuid.uuid4().hex[:6].upper(),
        "user_id": user["id"] if user else None,
        "customer": data.customer.model_dump(),
        "items": order_items,
        "subtotal": round(subtotal, 2),
        "total": round(subtotal, 2),
        "payment_method": "cod",
        "paid": False,
        "status": "recebida",
        "status_history": [{"status": "recebida", "at": now}],
        "created_at": now,
        "updated_at": now,
    }
    await db.orders.insert_one(order)
    order.pop("_id", None)
    return order


@router.get("/my/orders")
async def my_orders(request: Request):
    from security import get_current_user
    user = await get_current_user(request)
    orders = await db.orders.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return orders


@router.get("/orders/track")
async def track_order(code: str, phone: str):
    order = await db.orders.find_one(
        {"order_code": code.strip().upper(), "customer.phone": phone.strip()},
        {"_id": 0, "order_code": 1, "status": 1, "status_history": 1, "items": 1, "total": 1, "created_at": 1, "customer.name": 1},
    )
    if not order:
        raise HTTPException(status_code=404, detail="Encomenda nÃ£o encontrada. Verifica o cÃ³digo e o telefone.")
    return order


@router.get("/files/{path:path}")
async def serve_file(path: str):
    record = await db.files.find_one({"storage_path": path, "is_deleted": False}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="Ficheiro nÃ£o encontrado")
    try:
        data, content_type = get_object(path)
    except Exception:
        raise HTTPException(status_code=404, detail="Ficheiro nÃ£o encontrado")
    return Response(content=data, media_type=record.get("content_type") or content_type)
