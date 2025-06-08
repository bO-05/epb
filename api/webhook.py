import os
import json
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import base64
import traceback

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Log the incoming request path
            print(f"Received POST request to: {self.path}")
            
            if self.path != '/api/webhook':
                self.send_response(404)
                self.end_headers()
                return
            
            # Read the request body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Empty request body"}')
                return
                
            post_data = self.rfile.read(content_length)
            print(f"Received data: {post_data.decode()[:500]}...")  # Log first 500 chars
            
            # Parse JSON
            try:
                data = json.loads(post_data)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
                return
            
            # Process the email
            result = process_email(data)
            
            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
        except Exception as e:
            # Log the full error
            print(f"Error in webhook handler: {str(e)}")
            print(traceback.format_exc())
            
            # Send 500 error response
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            error_response = {"error": str(e), "type": type(e).__name__}
            self.wfile.write(json.dumps(error_response).encode())
    
    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status": "healthy", "endpoint": "/api/webhook"}')

def process_email(data):
    """Process incoming email from Postmark"""
    try:
        # Safely extract email fields
        from_full = data.get('FromFull', {})
        from_email = from_full.get('Email', '') if isinstance(from_full, dict) else ''
        
        # If FromFull is not present, try From field
        if not from_email:
            from_email = data.get('From', '')
            
        subject = data.get('Subject', '')
        text_body = data.get('StrippedTextReply') or data.get('TextBody', '')
        
        print(f"Processing email from: {from_email}, subject: {subject}")
        
        # Check if we have required environment variables
        required_vars = ['GITHUB_REPO', 'GITHUB_TOKEN', 'MISTRAL_API_KEY', 'POSTMARK_SERVER_TOKEN']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        
        if missing_vars:
            raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")
        
        if not text_body.strip():
            return {"status": "ignored", "reason": "empty body"}
        
        # For now, just return success to test the webhook
        # Comment out the actual processing until webhook works
        return {
            "status": "success",
            "message": "Webhook received",
            "from": from_email,
            "subject": subject,
            "body_length": len(text_body)
        }
        
        # Uncomment below when webhook is working
        # # Generate code using Mistral
        # files = generate_code_with_mistral(text_body)
        # 
        # # Create GitHub PR
        # pr_url, pr_number = create_github_pr(text_body, files)
        # 
        # # Send response email
        # send_email_response(from_email, pr_url, subject)
        # 
        # return {
        #     "status": "success",
        #     "pr_url": pr_url,
        #     "files_created": len(files)
        # }
        
    except Exception as e:
        print(f"Error in process_email: {str(e)}")
        print(traceback.format_exc())
        raise

def generate_code_with_mistral(instruction):
    """Generate code using Mistral AI"""
    headers = {
        "Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "codestral-latest",
        "messages": [
            {
                "role": "system",
                "content": """You are an expert developer. Generate clean, production-ready code.
                Return your response in this exact format:
                ---FILE: path/to/file.py---
                code content here
                ---END FILE---
                You can include multiple files."""
            },
            {
                "role": "user",
                "content": instruction
            }
        ],
        "temperature": 0.3,
        "max_tokens": 4000
    }
    
    response = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=25
    )
    response.raise_for_status()
    
    # Parse response
    content = response.json()["choices"][0]["message"]["content"]
    files = {}
    current_file = None
    current_content = []
    
    for line in content.split('\n'):
        if line.startswith('---FILE:') and line.endswith('---'):
            if current_file:
                files[current_file] = '\n'.join(current_content)
            current_file = line.replace('---FILE:', '').replace('---', '').strip()
            current_content = []
        elif line == '---END FILE---':
            if current_file:
                files[current_file] = '\n'.join(current_content)
            current_file = None
            current_content = []
        elif current_file:
            current_content.append(line)
    
    # Handle case where no files were parsed
    if not files:
        files = {"generated_code.py": content}
    
    return files

def create_github_pr(instruction, files):
    """Create a GitHub PR with the generated files"""
    headers = {
        "Authorization": f"token {os.environ.get('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    repo = os.environ.get('GITHUB_REPO')
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch_name = f"email-pr-{timestamp}"
    
    # Get default branch
    repo_response = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers=headers
    )
    repo_data = repo_response.json()
    default_branch = repo_data.get('default_branch', 'main')
    
    # Get latest commit SHA
    ref_response = requests.get(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{default_branch}",
        headers=headers
    )
    base_sha = ref_response.json()["object"]["sha"]
    
    # Create new branch
    requests.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        headers=headers,
        json={
            "ref": f"refs/heads/{branch_name}",
            "sha": base_sha
        }
    )
    
    # Create files in the new branch
    for filepath, content in files.items():
        encoded_content = base64.b64encode(content.encode()).decode()
        
        requests.put(
            f"https://api.github.com/repos/{repo}/contents/{filepath}",
            headers=headers,
            json={
                "message": f"Add {filepath} via email request",
                "content": encoded_content,
                "branch": branch_name
            }
        )
    
    # Create pull request
    pr_response = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers=headers,
        json={
            "title": f"[Email] {instruction[:60]}{'...' if len(instruction) > 60 else ''}",
            "body": f"""## 📧 Email-Generated PR

**Request:** {instruction}

**Generated by:** Mistral AI (codestral-latest)
**Files changed:** {len(files)}

---

This PR was automatically generated based on an email request.
The AI reviewer will analyze the changes shortly.

@coderabbitai please review""",
            "head": branch_name,
            "base": default_branch
        }
    )
    
    pr_data = pr_response.json()
    return pr_data["html_url"], pr_data["number"]

def send_email_response(to_email, pr_url, original_subject):
    """Send success response via Postmark"""
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #0070f3; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0;">✅ Pull Request Created!</h2>
        </div>
        
        <div style="padding: 20px; background: #f5f5f5;">
            <div style="background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                <h3 style="margin-top: 0;">Your PR is ready:</h3>
                <a href="{pr_url}" style="display: inline-block; background: #0070f3; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">{pr_url}</a>
            </div>
            
            <div style="background: white; padding: 20px; border-radius: 8px;">
                <h4 style="margin-top: 0;">What happens next?</h4>
                <ul style="color: #666;">
                    <li>The AI reviewer (ai-mistral-pr-reviewer) will analyze your code</li>
                    <li>You'll see comments and suggestions on the PR</li>
                    <li>Review and merge when ready</li>
                </ul>
                <p style="color: #666; margin-bottom: 0;">
                    💡 <strong>Tip:</strong> Reply to this email with more instructions to update the PR
                </p>
            </div>
        </div>
    </div>
    """
    
    response = requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "X-Postmark-Server-Token": os.environ.get('POSTMARK_SERVER_TOKEN'),
            "Content-Type": "application/json"
        },
        json={
            "From": "ai-coder@postmarkapp.com",
            "To": to_email,
            "Subject": f"Re: {original_subject}",
            "HtmlBody": html_body,
            "MessageStream": "outbound"
        }
    )
    response.raise_for_status()

def send_error_email(to_email, error_message, original_subject):
    """Send error notification"""
    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #ff0000; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0;">❌ Error Processing Request</h2>
        </div>
        <div style="padding: 20px; background: #f5f5f5;">
            <div style="background: white; padding: 20px; border-radius: 8px;">
                <p><strong>Error:</strong></p>
                <pre style="background: #f0f0f0; padding: 10px; border-radius: 4px; overflow-x: auto;">{error_message}</pre>
                <p style="color: #666;">Please check your request and try again.</p>
            </div>
        </div>
    </div>
    """
    
    requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "X-Postmark-Server-Token": os.environ.get('POSTMARK_SERVER_TOKEN'),
            "Content-Type": "application/json"
        },
        json={
            "From": "ai-coder@postmarkapp.com",
            "To": to_email,
            "Subject": f"Re: {original_subject} (Error)",
            "HtmlBody": html_body,
            "MessageStream": "outbound"
        }
    )
