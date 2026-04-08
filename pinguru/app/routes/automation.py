from fastapi import APIRouter, HTTPException, Depends
from app.database import get_db
from app.models.models import AutomationRuleCreate, PLAN_LIMITS
from app.routes.auth import get_current_user
from datetime import datetime
from bson import ObjectId

router = APIRouter()

def _to_str(doc: dict) -> dict:
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

@router.post("/rules")
async def create_rule(data: AutomationRuleCreate, db=Depends(get_db), user=Depends(get_current_user)):
    plan_limits = PLAN_LIMITS[user["plan"]]
    existing = await db.automation_rules.count_documents({"user_id": str(user["_id"])})
    if existing >= plan_limits["rules"]:
        raise HTTPException(status_code=403, detail=f"Rule limit reached. Upgrade your plan.")
    rule_doc = {
        "user_id": str(user["_id"]),
        "name": data.name,
        "trigger_type": data.trigger_type,
        "keywords": data.keywords,
        "reply_message": data.reply_message,
        "is_active": True,
        "sent_count": 0,
        "created_at": datetime.utcnow(),
    }
    result = await db.automation_rules.insert_one(rule_doc)
    rule_doc["_id"] = str(result.inserted_id)
    rule_doc["created_at"] = rule_doc["created_at"].isoformat()
    return {"rule": rule_doc}

@router.get("/rules")
async def list_rules(db=Depends(get_db), user=Depends(get_current_user)):
    rules = await db.automation_rules.find({"user_id": str(user["_id"])}).sort("created_at", -1).to_list(100)
    for r in rules:
        r["_id"] = str(r["_id"])
        if "created_at" in r: r["created_at"] = r["created_at"].isoformat()
    return {"rules": rules}

@router.patch("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    rule = await db.automation_rules.find_one({"_id": ObjectId(rule_id), "user_id": str(user["_id"])})
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    new_status = not rule["is_active"]
    await db.automation_rules.update_one({"_id": ObjectId(rule_id)}, {"$set": {"is_active": new_status}})
    return {"rule_id": rule_id, "is_active": new_status}

@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, data: AutomationRuleCreate, db=Depends(get_db), user=Depends(get_current_user)):
    result = await db.automation_rules.update_one(
        {"_id": ObjectId(rule_id), "user_id": str(user["_id"])},
        {"$set": {"name": data.name, "trigger_type": data.trigger_type, "keywords": data.keywords, "reply_message": data.reply_message}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"updated": True}

@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    result = await db.automation_rules.delete_one({"_id": ObjectId(rule_id), "user_id": str(user["_id"])})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": True}
