from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.models import Campaign, EmailMessage
from app.services.database import db_service
from app.services.email_provider_service import email_provider_service
from app.utils.helpers import generate_id, personalize_template
from datetime import datetime
import asyncio
from typing import List

router = APIRouter()

@router.post("/campaigns")
async def create_campaign(campaign: Campaign):
    """Create a new campaign"""
    campaign.id = generate_id()
    campaign_dict = campaign.dict()
    result = await db_service.create_campaign(campaign_dict)
    campaign_dict.pop('_id', None)
    return campaign_dict

@router.get("/campaigns")
async def get_campaigns():
    """Get all campaigns"""
    campaigns = await db_service.get_campaigns()
    return campaigns

@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    """Get a specific campaign by ID"""
    campaign = await db_service.get_campaign_by_id(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign

@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(campaign_id: str, background_tasks: BackgroundTasks, send_data: dict = {}):
    """Send campaign emails"""
    campaign = await db_service.get_campaign_by_id(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    template = await db_service.get_template_by_id(campaign["template_id"])
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Get prospects from campaign lists
    prospects = []
    for list_id in campaign.get("list_ids", []):
        list_prospects = await db_service.get_prospects_by_list_id(list_id)
        prospects.extend(list_prospects)
    
    # Remove duplicates
    seen_emails = set()
    unique_prospects = []
    for prospect in prospects:
        if prospect["email"] not in seen_emails:
            seen_emails.add(prospect["email"])
            unique_prospects.append(prospect)
    
    # Limit to max_emails
    max_emails = campaign.get("max_emails", 100)
    prospects = unique_prospects[:max_emails]
    
    # Schedule email sending
    background_tasks.add_task(process_campaign_emails, campaign_id, prospects, template)
    
    # Update campaign status
    await db_service.update_campaign(campaign_id, {
        "status": "active", 
        "prospect_count": len(prospects)
    })
    
    return {
        "campaign_id": campaign_id,
        "status": "active",
        "total_prospects": len(prospects),
        "message": f"Campaign started. Sending to {len(prospects)} prospects"
    }

async def process_campaign_emails(campaign_id: str, prospects: List[dict], template: dict):
    """Process campaign emails in background"""
    sent_count = 0
    failed_count = 0
    
    # Get default email provider
    default_provider = await email_provider_service.get_default_provider()
    if not default_provider:
        print(f"No default email provider found for campaign {campaign_id}")
        return
    
    for prospect in prospects:
        try:
            # Personalize email content
            personalized_content = personalize_template(template["content"], prospect)
            personalized_subject = personalize_template(template["subject"], prospect)
            
            # Send email using email provider service
            success, error = await email_provider_service.send_email(
                default_provider["id"],
                prospect["email"],
                personalized_subject,
                personalized_content
            )
            
            if not success and error:
                print(f"Email sending failed to {prospect['email']}: {error}")
            
            # Create email record
            email_record = EmailMessage(
                id=generate_id(),
                prospect_id=prospect["id"],
                campaign_id=campaign_id,
                subject=personalized_subject,
                content=personalized_content,
                status="sent" if success else "failed",
                sent_at=datetime.utcnow() if success else None
            )
            
            await db_service.create_email_record(email_record.dict())
            
            if success:
                sent_count += 1
            else:
                failed_count += 1
            
            # Update prospect last contact
            await db_service.update_prospect_last_contact(prospect["id"], datetime.utcnow())
            
            # Small delay to avoid overwhelming SMTP server
            await asyncio.sleep(0.1)
            
        except Exception as e:
            print(f"Error sending email to {prospect['email']}: {str(e)}")
            failed_count += 1
            continue
    
    # Update campaign with final results
    await db_service.update_campaign(campaign_id, {"status": "completed"})
    
    print(f"Campaign {campaign_id} completed: {sent_count} sent, {failed_count} failed")