import os
import json
import time
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import base64
import traceback

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            if self.path != '/api/webhook':
                self.send_response(404)
                self.end_headers()
                return
            
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Empty request body"}')
                return
                
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data)
            except json.JSONDecodeError as e:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
                return
            
            # Check if this is a test webhook from Postmark
            if self.is_test_webhook(data):
                # For test webhooks, just return 200 OK
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "message": "Test webhook received"}).encode())
                return
            
            # Process real email
            result = process_email(data)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
        except Exception as e:
            print(f"Error in webhook handler: {str(e)}")
            print(traceback.format_exc())
            
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            error_response = {"error": str(e), "type": type(e).__name__}
            self.wfile.write(json.dumps(error_response).encode())
    
    def is_test_webhook(self, data):
        """Check if this is a test webhook from Postmark"""
        # Postmark test webhooks have specific characteristics
        # They often have minimal data or specific test patterns
        
        # Check for common test webhook indicators
        if not data:
            return True
            
        # If it's missing critical fields, it's likely a test
        if 'From' not in data and 'FromFull' not in data:
            return True
            
        # Postmark's "Check" button sends a minimal test payload
        # Real emails always have these fields
        required_fields = ['From', 'Subject', 'MessageID']
        if not all(field in data for field in required_fields):
            return True
            
        return False
    
    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status": "healthy", "endpoint": "/api/webhook"}')

def process_email(data):
    """Process incoming email from Postmark"""
    try:
        # Extract email fields safely
        from_full = data.get('FromFull', {})
        from_email = from_full.get('Email', '') if isinstance(from_full, dict) else ''
        
        # Fallback to From field if FromFull is not present
        if not from_email:
            from_email = data.get('From', '')
            # Extract email from "Name <email@domain.com>" format
            if '<' in from_email and '>' in from_email:
                from_email = from_email.split('<')[1].split('>')[0]
        
        subject = data.get('Subject', '')
        text_body = data.get('StrippedTextReply') or data.get('TextBody', '')
        message_id = data.get('MessageID', '')
        
        print(f"Processing email - From: {from_email}, Subject: {subject}, MessageID: {message_id}")
        
        # Validate we have minimum required data
        if not from_email:
            return {"status": "error", "reason": "No sender email found"}
            
        if not text_body.strip():
            return {"status": "ignored", "reason": "Empty email body"}
        
        # Check environment variables
        required_vars = ['GITHUB_REPO', 'GITHUB_TOKEN', 'MISTRAL_API_KEY', 'POSTMARK_SERVER_TOKEN']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        
        if missing_vars:
            error_msg = f"Missing environment variables: {', '.join(missing_vars)}"
            print(f"Error: {error_msg}")
            # Still return 200 to Postmark, but log the error
            return {"status": "error", "reason": error_msg}
        
        # Generate code using Mistral
        print(f"Calling Mistral AI with instruction: {text_body[:100]}...")
        files = generate_code_with_mistral(text_body)
        
        # Create GitHub PR
        print(f"Creating GitHub PR with {len(files)} files...")
        pr_url, pr_number = create_github_pr(text_body, files)
        
        # Send response email
        print(f"Sending response email to {from_email}...")
        send_email_response(from_email, pr_url, subject)
        
        return {
            "status": "success",
            "pr_url": pr_url,
            "files_created": len(files),
            "recipient": from_email
        }
        
    except Exception as e:
        print(f"Error in process_email: {str(e)}")
        print(traceback.format_exc())
        
        # Try to send error notification
        try:
            if 'from_email' in locals() and from_email:
                send_error_email(from_email, str(e), subject if 'subject' in locals() else "Error")
        except:
            pass
            
        # Return error but with 200 status to prevent Postmark retries
        return {
            "status": "error",
            "reason": str(e),
            "type": type(e).__name__
        }

def generate_code_with_mistral(instruction):
    """Generate code using Mistral AI"""
    try:
        headers = {
            "Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "codestral-latest",  
            "messages": [
                {
                    "role": "system",
                    "content": """You are an expert software developer with deep knowledge of best practices, design patterns, and modern coding standards.

Generate clean, production-ready code based on the user's requirements with these guidelines:
1. Write well-structured, maintainable code with appropriate error handling
2. Include clear comments for complex logic and document function purposes
3. Follow language-specific best practices and conventions
4. Ensure security considerations are addressed
5. Use semantic naming and consistent formatting
6. Prioritize performance and efficiency where applicable

Return your response in this EXACT format for parsing:
---FILE: path/to/file.extension---
code content here
---END FILE---

For multiple files, repeat the format for each file. Use appropriate file extensions for the language (e.g., .py for Python, .js for JavaScript, .html for HTML, etc.). Ensure file paths follow standard conventions for the language. Include all necessary imports.

If you're creating a complete application:
- Include necessary configuration files (.env.example, requirements.txt, package.json, etc.)
- Add a README.md with setup and usage instructions
- Consider appropriate folder structure for the project type."""
                },
                {
                    "role": "user",
                    "content": instruction
                }
            ],
            "temperature": 0.3,
            "max_tokens": 4000
        }
        
        print(f"Calling Mistral API with instruction: {instruction[:100]}...")
        
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=25
        )
        
        if response.status_code != 200:
            raise Exception(f"Mistral API error: {response.status_code} - {response.text}")
            
        response_data = response.json()
        content = response_data["choices"][0]["message"]["content"]
        
        print(f"Mistral response received, parsing files...")
        
        # Parse response
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
        
        # If no files were parsed, treat entire response as code
        if not files:
            files = {"generated_code.py": content}
        
        print(f"Parsed {len(files)} files from Mistral response")
        return files
        
    except Exception as e:
        print(f"Error in generate_code_with_mistral: {str(e)}")
        raise

def create_github_pr(instruction: str, files: dict[str, str]):
    """
    Create a GitHub branch + commit the AI-generated files + open PR.

    Fixes the 422 “sha wasn’t supplied” error by always fetching the
    current SHA of a file that already exists on the default branch.
    """
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github.v3+json",
    }
    repo      = os.environ["GITHUB_REPO"]          # owner/repo
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch    = f"email-pr-{timestamp}"

    # --- get default branch + base SHA ---------------------------------
    repo_json   = requests.get(f"https://api.github.com/repos/{repo}", headers=headers).json()
    default     = repo_json.get("default_branch", "main")
    base_sha    = requests.get(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{default}",
        headers=headers
    ).json()["object"]["sha"]

    # --- create branch -------------------------------------------------
    requests.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    ).raise_for_status()

    # --- commit each file ---------------------------------------------
    for path, content in files.items():
        # Skip giant or forbidden files
        if path.lower() in {"license", "license.md"}:
            continue

        b64 = base64.b64encode(content.encode()).decode()

        # Does the file already exist on *default* branch?
        sha = None
        head = requests.get(
            f"https://api.github.com/repos/{repo}/contents/{path}?ref={default}",
            headers=headers,
        )
        if head.status_code == 200:
            sha = head.json()["sha"]

        body = {
            "message": f"Add/update {path} via email instruction",
            "content": b64,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha                           # <-- important!

        put = requests.put(
            f"https://api.github.com/repos/{repo}/contents/{path}",
            headers=headers,
            json=body,
        )
        if put.status_code not in (200, 201):
            raise RuntimeError(f"GitHub error {path}: {put.status_code} – {put.text}")

    # --- open PR -------------------------------------------------------
    pr = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers=headers,
        json={
            "title": f"[Email] {instruction[:60]}{'...' if len(instruction) > 60 else ''}",
            "body":  f"Generated from email:\n\n> {instruction}",
            "head":  branch,
            "base":  default,
        },
    ).json()

    return pr["html_url"], pr["number"]

def send_email_response(to_email, pr_url, original_subject):
    """Send success response via Postmark"""
    try:
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #0070f3; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                <h2 style="margin: 0;">✅ Pull Request Created!</h2>
            </div>
            
            <div style="padding: 20px; background: #f5f5f5;">
                <div style="background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                    <h3 style="margin-top: 0;">Your PR is ready:</h3>
                    <a href="{pr_url}" style="display: inline-block; background: #0070f3; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">View Pull Request</a>
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
                "From": "icarus@hidrokultur.com",  
                "To": to_email,
                "Subject": f"Re: {original_subject}",
                "HtmlBody": html_body,
                "MessageStream": "outbound"
            }
        )
        
        response.raise_for_status()
        print(f"Email sent successfully to {to_email}")
        
    except Exception as e:
        print(f"Error in send_email_response: {str(e)}")

def send_error_email(to_email, error_message, original_subject):
    """Send error notification"""
    try:
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #ff0000; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                <h2 style="margin: 0;">❌ Error Processing Request</h2>
            </div>
            <div style="padding: 20px; background: #f5f5f5;">
                <div style="background: white; padding: 20px; border-radius: 8px;">
                    <p><strong>Error:</strong></p>
                    <pre style="background: #f0f0f0; padding: 10px; border-radius: 4px; overflow-x: auto;">{error_message}</pre>
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
                "From": "icarus@hidrokultur.com",  # YOUR VERIFIED EMAIL!
                "To": to_email,
                "Subject": f"Re: {original_subject} (Error)",
                "HtmlBody": html_body,
                "MessageStream": "outbound"
            }
        )
    except:
        pass
