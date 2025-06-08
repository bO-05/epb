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
        
        # Using the correct model name from Mistral's current lineup
        payload = {
            "model": "codestral-latest",  # Updated model name
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

def create_github_pr(instruction, files):
    """Create a GitHub PR with the generated files"""
    try:
        headers = {
            "Authorization": f"token {os.environ.get('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        repo = os.environ.get('GITHUB_REPO')
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch_name = f"email-pr-{timestamp}"
        
        print(f"Creating PR in repo: {repo}, branch: {branch_name}")
        
        # Get default branch
        repo_response = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=headers
        )
        
        if repo_response.status_code != 200:
            raise Exception(f"GitHub API error getting repo: {repo_response.status_code} - {repo_response.text}")
            
        repo_data = repo_response.json()
        default_branch = repo_data.get('default_branch', 'main')
        
        # Get latest commit SHA
        ref_response = requests.get(
            f"https://api.github.com/repos/{repo}/git/refs/heads/{default_branch}",
            headers=headers
        )
        
        if ref_response.status_code != 200:
            raise Exception(f"GitHub API error getting ref: {ref_response.status_code} - {ref_response.text}")
            
        base_sha = ref_response.json()["object"]["sha"]
        
        # Create new branch
        branch_response = requests.post(
            f"https://api.github.com/repos/{repo}/git/refs",
            headers=headers,
            json={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha
            }
        )
        
        if branch_response.status_code != 201:
            raise Exception(f"GitHub API error creating branch: {branch_response.status_code} - {branch_response.text}")
        
        # Create files in the new branch
        for filepath, content in files.items():
            print(f"Creating file: {filepath}")
            encoded_content = base64.b64encode(content.encode()).decode()
            
            file_response = requests.put(
                f"https://api.github.com/repos/{repo}/contents/{filepath}",
                headers=headers,
                json={
                    "message": f"Add {filepath} via email request",
                    "content": encoded_content,
                    "branch": branch_name
                }
            )
            
            if file_response.status_code not in [200, 201]:
                raise Exception(f"GitHub API error creating file {filepath}: {file_response.status_code} - {file_response.text}")
        
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
The AI reviewer will analyze the changes shortly.""",
                "head": branch_name,
                "base": default_branch
            }
        )
        
        if pr_response.status_code != 201:
            raise Exception(f"GitHub API error creating PR: {pr_response.status_code} - {pr_response.text}")
        
        pr_data = pr_response.json()
        print(f"PR created successfully: {pr_data['html_url']}")
        return pr_data["html_url"], pr_data["number"]
        
    except Exception as e:
        print(f"Error in create_github_pr: {str(e)}")
        raise

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
                
                <div style="background: white; padding: 20px; border-radius: 8px;">
                    <h4 style="margin-top: 0;">What happens next?</h4>
                    <ul style="color: #666;">
                        <li>The AI reviewer will analyze your code</li>
                        <li>You'll see comments and suggestions on the PR</li>
                        <li>Review and merge when ready</li>
                    </ul>
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
        
        if response.status_code != 200:
            raise Exception(f"Postmark API error: {response.status_code} - {response.text}")
            
        print(f"Email sent successfully to {to_email}")
        
    except Exception as e:
        print(f"Error in send_email_response: {str(e)}")
        raise

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
                "From": "ai-coder@postmarkapp.com",
                "To": to_email,
                "Subject": f"Re: {original_subject} (Error)",
                "HtmlBody": html_body,
                "MessageStream": "outbound"
            }
        )
    except:
        pass  # Don't raise errors from error handler
