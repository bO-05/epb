import os
import json
import time
import requests
import re
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
        if not data:
            return True
            
        if 'From' not in data and 'FromFull' not in data:
            return True
            
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

def extract_repo_from_email(subject, body):
    """Extract repository from email content using various patterns"""
    patterns = [
        r'repo:\s*([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)',
        r'@([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)',
        r'github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)',
        r'target:\s*([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)',
        r'project:\s*([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)',
    ]
    
    text = f"{subject} {body}"
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None

def get_repo_for_user(from_email):
    """Map email addresses to default repositories"""
    email_to_repo = {
        "icarus@hidrokultur.com": "bO-05/mailforge-test-target",
        "pr-creator@hidrokultur.com": "bO-05/mailforge-test-target",
        "dev@hidrokultur.com": "bO-05/mailforge-test-target",
        # Add more mappings as needed
    }
    
    # Check exact match first
    if from_email in email_to_repo:
        return email_to_repo[from_email]
    
    # Check domain-based mapping
    domain = from_email.split('@')[-1] if '@' in from_email else ''
    if domain == 'hidrokultur.com':
        return "bO-05/mailforge-test-target"
    
    return None

def validate_repo_access(repo, headers):
    """Check if we have access to the repository"""
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=headers,
            timeout=10
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Error validating repo access: {e}")
        return False

def get_repo_context(repo, headers, instruction):
    """Fetch relevant files from the repo to provide context to the AI"""
    try:
        print(f"Fetching repository context for {repo}...")
        
        # Get repository structure
        tree_response = requests.get(
            f"https://api.github.com/repos/{repo}/git/trees/main?recursive=1",
            headers=headers,
            timeout=15
        )
        
        if tree_response.status_code != 200:
            print(f"Failed to get repo tree: {tree_response.status_code}")
            return ""
            
        tree_data = tree_response.json()
        
        # Filter relevant files based on instruction and common patterns
        relevant_files = []
        instruction_lower = instruction.lower()
        
        # Keywords that indicate important files
        important_keywords = [
            'main.py', 'app.py', '__init__.py', 'requirements.txt',
            'config', 'model', 'api', 'endpoint', 'route', 'server',
            'fastapi', 'flask', 'django', 'package.json', 'setup.py'
        ]
        
        # Look for existing related files
        for item in tree_data.get('tree', []):
            if item['type'] == 'blob' and item.get('size', 0) < 50000:  # Skip large files
                path = item['path']
                filename = path.split('/')[-1].lower()
                
                # Include files that might be relevant
                should_include = False
                
                # Check if it's an important file
                if any(keyword in filename for keyword in important_keywords):
                    should_include = True
                
                # Check if instruction mentions this file or related terms
                if any(keyword in instruction_lower for keyword in [
                    filename.replace('.py', ''),
                    filename.replace('.js', ''),
                    path.lower()
                ]):
                    should_include = True
                
                # Include Python files in root or main directories
                if (filename.endswith('.py') and 
                    ('/' not in path or path.startswith(('src/', 'app/', 'api/')))):
                    should_include = True
                
                if should_include:
                    relevant_files.append(path)
        
        # Limit to most relevant files to prevent token overflow
        relevant_files = relevant_files[:8]
        
        if not relevant_files:
            return "## Repository Context:\nNo relevant existing files found. This appears to be a new project or the files are in subdirectories not yet explored.\n\n"
        
        # Fetch content of relevant files
        context = "## Existing Repository Structure and Code:\n\n"
        context += f"**Repository:** {repo}\n"
        context += f"**Relevant files found:** {len(relevant_files)}\n\n"
        
        for file_path in relevant_files:
            try:
                file_response = requests.get(
                    f"https://api.github.com/repos/{repo}/contents/{file_path}",
                    headers=headers,
                    timeout=10
                )
                
                if file_response.status_code == 200:
                    file_data = file_response.json()
                    if file_data.get('size', 0) < 10000:  # Skip large files
                        try:
                            content = base64.b64decode(file_data['content']).decode('utf-8')
                            context += f"### {file_path}\n```python\n{content}\n```\n\n"
                        except UnicodeDecodeError:
                            context += f"### {file_path}\n*Binary file - content not shown*\n\n"
                    else:
                        context += f"### {file_path}\n*File too large - content not shown*\n\n"
                else:
                    print(f"Failed to fetch {file_path}: {file_response.status_code}")
            except Exception as e:
                print(f"Error fetching file {file_path}: {e}")
                continue
        
        print(f"Repository context gathered: {len(relevant_files)} files")
        return context
        
    except Exception as e:
        print(f"Error getting repo context: {e}")
        return "## Repository Context:\nUnable to fetch repository context due to an error.\n\n"

def process_email(data):
    """Enhanced email processing with context and dynamic repos"""
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
        
        # Determine target repository
        dynamic_repo = extract_repo_from_email(subject, text_body)
        if dynamic_repo:
            target_repo = dynamic_repo
            print(f"Using dynamic repo from email: {target_repo}")
        else:
            target_repo = get_repo_for_user(from_email) or os.environ.get("GITHUB_REPO", "bO-05/mailforge-test-target")
            print(f"Using default repo for user: {target_repo}")
        
        # Validate access to target repository
        headers = {
            "Authorization": f"token {os.environ.get('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github.v3+json",
        }
        
        if not validate_repo_access(target_repo, headers):
            error_msg = f"No access to repository: {target_repo}"
            print(f"Error: {error_msg}")
            send_error_email(from_email, error_msg, subject)
            return {"status": "error", "reason": error_msg}
        
        # Check other required environment variables
        required_vars = ['GITHUB_TOKEN', 'MISTRAL_API_KEY', 'POSTMARK_SERVER_TOKEN']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        
        if missing_vars:
            error_msg = f"Missing environment variables: {', '.join(missing_vars)}"
            print(f"Error: {error_msg}")
            return {"status": "error", "reason": error_msg}
        
        # Get repository context for AI
        repo_context = get_repo_context(target_repo, headers, text_body)
        
        # Generate context-aware code using Mistral
        print(f"Calling Mistral AI with instruction and repo context...")
        files = generate_code_with_mistral(text_body, repo_context)
        
        # Create GitHub PR in the target repository
        print(f"Creating GitHub PR in {target_repo} with {len(files)} files...")
        pr_url, pr_number = create_github_pr(text_body, files, target_repo)
        
        # Send response email
        print(f"Sending response email to {from_email}...")
        send_email_response(from_email, pr_url, subject, target_repo)
        
        return {
            "status": "success",
            "pr_url": pr_url,
            "repository": target_repo,
            "files_created": len(files),
            "recipient": from_email,
            "context_files": len(repo_context.split('###')) - 1 if '###' in repo_context else 0
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

def generate_code_with_mistral(instruction, repo_context=""):
    """Enhanced code generation with repository context"""
    try:
        headers = {
            "Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}",
            "Content-Type": "application/json"
        }
        
        # Enhanced system prompt with context awareness
        system_prompt = f"""You are an expert software developer with deep knowledge of best practices, design patterns, and modern coding standards.

{repo_context}

Based on the existing repository structure and code above, generate code that:

1. **Integrates seamlessly** with the existing codebase and follows established patterns
2. **Reuses existing imports, configurations, and utilities** where appropriate
3. **Maintains consistency** with the current architecture and coding style
4. **Updates existing files** when modifications are needed rather than creating duplicates
5. **Follows the project's conventions** for file organization and naming
6. **Implements proper error handling** and logging consistent with existing code
7. **Includes appropriate documentation** and comments matching the project style

**Code Quality Guidelines:**
- Write clean, maintainable, production-ready code
- Use semantic naming and consistent formatting
- Implement proper security considerations
- Optimize for performance where applicable
- Include comprehensive error handling
- Add clear comments for complex logic

**File Generation Rules:**
- Only create files that are strictly necessary for the requested functionality
- Do NOT generate README.md, LICENSE, or .gitignore unless specifically requested
- Do NOT create requirements.txt unless adding new dependencies
- Update existing files when appropriate rather than creating new ones
- Use appropriate file extensions and follow project structure

Return your response in this EXACT format for parsing:
---FILE: path/to/file.extension---
code content here
---END FILE---

For multiple files, repeat the format for each file. Ensure file paths follow the project's existing structure.

IMPORTANT: Do NOT include markdown formatting symbols like triple backticks (```) or language identifiers in the actual code content."""

        payload = {
            "model": "codestral-latest",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": instruction}
            ],
            "temperature": 0.2,  # Lower temperature for more consistent code
            "max_tokens": 50000
        }
        
        print(f"Calling Mistral API with enhanced context...")
        
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code != 200:
            raise Exception(f"Mistral API error: {response.status_code} - {response.text}")
            
        response_data = response.json()
        content = response_data["choices"][0]["message"]["content"]
        
        print(f"Mistral response received, parsing files...")
        
        # Parse response to extract files
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
        
        # Handle case where no files were parsed properly
        if not files:
            # Try to extract code blocks
            import re
            code_blocks = re.findall(r'```(?:[\w-]+)?\n(.*?)\n```', content, re.DOTALL)
            if code_blocks:
                # Get filename from content or generate default
                filename_match = re.search(r'(?:[\w-]+\.[\w-]+)', content.split('\n')[0])
                filename = filename_match.group(0) if filename_match else "generated_code.py"
                files = {filename: code_blocks[0]}
            else:
                files = {"generated_code.py": content}
        
        # Clean up any markdown formatting in code content
        cleaned_files = {}
        for filepath, file_content in files.items():
            # Strip markdown code block indicators if present
            cleaned_content = file_content
            # Remove leading ```language and trailing ``` if they exist
            cleaned_content = re.sub(r'^```[\w-]*\n', '', cleaned_content)
            cleaned_content = re.sub(r'\n```$', '', cleaned_content)
            
            # Filter out unwanted files
            filename = filepath.split('/')[-1].lower()
            if filename in ['readme.md', 'license', 'license.md', '.gitignore'] and len(files) > 1:
                print(f"Skipping auto-generated file: {filepath}")
                continue
                
            cleaned_files[filepath] = cleaned_content
        
        print(f"Parsed and cleaned {len(cleaned_files)} files from Mistral response")
        return cleaned_files
        
    except Exception as e:
        print(f"Error in generate_code_with_mistral: {str(e)}")
        raise

def create_github_pr(instruction: str, files: dict[str, str], target_repo: str):
    """
    Create a GitHub branch + commit the AI-generated files + open PR in specified repo.
    Handles existing files by fetching their SHA to avoid 422 errors.
    """
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"email-pr-{timestamp}"

    print(f"Creating PR in repository: {target_repo}")

    # Get default branch + base SHA
    repo_json = requests.get(f"https://api.github.com/repos/{target_repo}", headers=headers).json()
    default = repo_json.get("default_branch", "main")
    base_sha = requests.get(
        f"https://api.github.com/repos/{target_repo}/git/refs/heads/{default}",
        headers=headers
    ).json()["object"]["sha"]

    # Create branch
    branch_response = requests.post(
        f"https://api.github.com/repos/{target_repo}/git/refs",
        headers=headers,
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    
    if branch_response.status_code != 201:
        raise Exception(f"Failed to create branch: {branch_response.status_code} - {branch_response.text}")

    # Commit each file
    for path, content in files.items():
        print(f"Creating/updating file: {path}")
        
        # Skip problematic files
        if path.lower() in {"license", "license.md"} and len(files) > 1:
            continue

        b64 = base64.b64encode(content.encode()).decode()

        # Check if file already exists on default branch
        sha = None
        head = requests.get(
            f"https://api.github.com/repos/{target_repo}/contents/{path}?ref={default}",
            headers=headers,
        )
        if head.status_code == 200:
            sha = head.json()["sha"]
            print(f"File {path} exists, using SHA: {sha[:8]}...")

        body = {
            "message": f"Add/update {path} via email instruction",
            "content": b64,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        put = requests.put(
            f"https://api.github.com/repos/{target_repo}/contents/{path}",
            headers=headers,
            json=body,
        )
        if put.status_code not in (200, 201):
            raise RuntimeError(f"GitHub error {path}: {put.status_code} – {put.text}")

    # Open PR
    pr_body = f"""## 📧 Email-Generated Pull Request

**Instruction:** {instruction}

**Repository:** {target_repo}
**Generated by:** Mistral AI (codestral-latest) with repository context
**Files changed:** {len(files)}

---

This PR was automatically generated based on an email request with full awareness of the existing codebase structure and patterns.

The AI has analyzed the existing repository and generated code that:
- Follows established patterns and conventions
- Integrates seamlessly with existing code
- Maintains consistency with current architecture
- Reuses existing utilities and configurations

**Files in this PR:**
{chr(10).join(f'- `{path}`' for path in files.keys())}

@coderabbitai please review this PR thoroughly, focusing on integration with existing code."""

    pr = requests.post(
        f"https://api.github.com/repos/{target_repo}/pulls",
        headers=headers,
        json={
            "title": f"[Email] {instruction[:60]}{'...' if len(instruction) > 60 else ''}",
            "body": pr_body,
            "head": branch,
            "base": default,
        },
    ).json()

    return pr["html_url"], pr["number"]

def send_email_response(to_email, pr_url, original_subject, repository):
    """Send success response via Postmark with repository info"""
    try:
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #0070f3; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                <h2 style="margin: 0;">✅ Pull Request Created!</h2>
            </div>
            
            <div style="padding: 20px; background: #f5f5f5;">
                <div style="background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                    <h3 style="margin-top: 0;">Your PR is ready:</h3>
                    <p><strong>Repository:</strong> {repository}</p>
                    <a href="{pr_url}" style="display: inline-block; background: #0070f3; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">View Pull Request</a>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 8px;">
                    <h4 style="margin-top: 0;">What happens next?</h4>
                    <ul style="color: #666;">
                        <li>🤖 The AI reviewer will analyze your code with repository context</li>
                        <li>📝 You'll see detailed comments and suggestions on the PR</li>
                        <li>🔍 Code has been generated with awareness of existing patterns</li>
                        <li>✅ Review and merge when ready</li>
                    </ul>
                    <p style="color: #666; margin-bottom: 0;">
                        💡 <strong>Tip:</strong> Reply to this email with "repo:owner/name" to target a different repository
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
                "From": "pr-creator@hidrokultur.com",
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
    """Send error notification with helpful information"""
    try:
        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #ff0000; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                <h2 style="margin: 0;">❌ Error Processing Request</h2>
            </div>
            <div style="padding: 20px; background: #f5f5f5;">
                <div style="background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                    <p><strong>Error:</strong></p>
                    <pre style="background: #f0f0f0; padding: 10px; border-radius: 4px; overflow-x: auto;">{error_message}</pre>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 8px;">
                    <h4 style="margin-top: 0;">Troubleshooting Tips:</h4>
                    <ul style="color: #666;">
                        <li>Ensure you have access to the target repository</li>
                        <li>Use "repo:owner/name" in your email to specify a different repository</li>
                        <li>Check that your email contains clear coding instructions</li>
                        <li>Try again with a simpler request</li>
                    </ul>
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
                "From": "pr-creator@hidrokultur.com",
                "To": to_email,
                "Subject": f"Re: {original_subject} (Error)",
                "HtmlBody": html_body,
                "MessageStream": "outbound"
            }
        )
    except:
        pass
