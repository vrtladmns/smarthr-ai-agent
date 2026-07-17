# Company Policy Voice Agent

This project is a local RAG agent over `documents/company_policy.pdf`. It now has:

- `agent.py`: text chat RAG loop
- `voice_agent.py`: voice calling loop with speech-to-text, RAG answer, text-to-speech, and barge-in interruption
- LangSmith tracing through the existing `.env` values

## Run

Index the policy if needed:

```bash
./venv/bin/python ingest.py
```

Text mode:

```bash
./venv/bin/python agent.py
```

Voice mode:

```bash
./venv/bin/python voice_agent.py
```

## Speed Tuning

The defaults are tuned for faster replies:

```bash
WHISPER_MODEL=tiny.en
VOICE_SILENCE_SECONDS=0.55
RAG_TOP_K=3
OLLAMA_NUM_PREDICT=160
```

If it is still slow, use a smaller Ollama chat model in `.env`, for example:

```bash
OLLAMA_CHAT_MODEL=qwen2.5:3b
```

The voice runner prints timings for each turn by default:

```text
[timing] listen=... stt=... rag=... tts+play=...
```

Set `VOICE_SHOW_TIMINGS=false` to hide them.

## Voice Dependencies

The Python packages are already present in the virtualenv, but microphone capture also needs the system PortAudio library. On Ubuntu/Debian:

```bash
sudo apt-get install portaudio19-dev libportaudio2
```

`voice_agent.py` uses:

- `sounddevice` for microphone input
- `webrtcvad` to detect speech and silence
- `faster-whisper` for speech-to-text
- `edge-tts` and `pygame` for spoken replies

## LangSmith

The existing `.env` enables tracing:

```bash
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT="Voice RAG Agent"
```

Each voice turn creates LangSmith spans for speech-to-text, the RAG call, and text-to-speech.

## LLM Provider

The agents can use Ollama locally or DeepSeek through its OpenAI-compatible API. DeepSeek Flash is configured by default:

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-deepseek-key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MAX_TOKENS=220
```

To switch back to local Ollama:

```bash
LLM_PROVIDER=ollama
OLLAMA_CHAT_MODEL=qwen2.5:7b
```

## AI Recruiter Agent

`recruiter_agent.py` processes unread inbox emails, checks whether they are employment related, validates CV attachments, evaluates CVs, matches them to open PostgreSQL requirements, saves candidate/application details, and replies to the applicant.

Install recruiter extras:

```bash
./venv/bin/python -m pip install -r requirements-recruiter.txt
```

For this AWS recruiter-agent-only Compose file, use an external database in `.env`. If you re-enable the commented local PostgreSQL service for development, start it with:

```bash
docker compose up -d recruiter-postgres
```

Or run the full setup helper:

```bash
./scripts/setup_recruiter_db.sh
```

The local DB connection is already added to `.env`:

```bash
DB_PROVIDER=postgres
DATABASE_URL=postgresql://recruiter:recruiter_password@localhost:5432/recruitment
```

To use SQL Server instead, switch the provider and set the connection string:

```bash
DB_PROVIDER=mssql
MSSQL_CONNECTION_STRING="Server=your-sql-server; Database=your-db; User Id=your-user; Password=your-password; Encrypt=True; TrustServerCertificate=True; MultipleActiveResultSets=True;"
# Optional, leave blank for auto-detect:
MSSQL_ODBC_DRIVER="ODBC Driver 18 for SQL Server"
```

The app also accepts common .NET-style SQL Server strings such as `User Id=...`, `Password=...`, `Encrypt=True`, `TrustServerCertificate=True`, and `MultipleActiveResultSets=True`; these are normalized for `pyodbc`.

The agent and dashboard use the same tables on either database. PostgreSQL stores JSON fields as `JSONB`; SQL Server stores the same JSON values as `NVARCHAR(MAX)`.

Local SQL Server mode uses `pyodbc`, so your machine must have Microsoft ODBC Driver 18 for SQL Server and unixODBC installed. On Ubuntu/Debian:

```bash
sudo ./scripts/install_mssql_odbc_ubuntu.sh
```

On newer Ubuntu releases where Microsoft has not published a matching repo yet, the installer falls back to Microsoft's Ubuntu 24.04 package feed.

Verify the driver is registered:

```bash
./venv/bin/python -c "import pyodbc; print(pyodbc.drivers())"
```

You should see `ODBC Driver 18 for SQL Server` or another SQL Server driver in the list. If the installed driver has a different name, put that exact name in `MSSQL_ODBC_DRIVER`.

The Docker image installs that driver automatically.

Required email `.env` values:

```bash
MAIL_PROVIDER=gmail_imap
RECRUITER_IMAP_HOST=imap.example.com
RECRUITER_IMAP_PORT=993
RECRUITER_EMAIL=hr@example.com
RECRUITER_EMAIL_PASSWORD=app-password
RECRUITER_SMTP_HOST=smtp.example.com
RECRUITER_SMTP_PORT=587
RECRUITER_FROM_EMAIL=hr@example.com
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT="AI Recruiter Agent"
```

Mail providers:

```bash
# Pick exactly one provider with MAIL_PROVIDER.
# No code change is needed when switching mailboxes.

# Gmail over IMAP/SMTP, supports Gmail thread lookup
MAIL_PROVIDER=gmail_imap
RECRUITER_IMAP_HOST=imap.gmail.com
RECRUITER_SMTP_HOST=smtp.gmail.com
RECRUITER_EMAIL=hr@gmail.com
RECRUITER_EMAIL_PASSWORD=gmail-app-password
RECRUITER_FROM_EMAIL=hr@gmail.com

# Outlook / Microsoft 365 over IMAP/SMTP
MAIL_PROVIDER=outlook_imap
RECRUITER_IMAP_HOST=outlook.office365.com
RECRUITER_SMTP_HOST=smtp.office365.com

# Outlook / Microsoft 365 through Microsoft Graph API
MAIL_PROVIDER=microsoft_graph
MICROSOFT_TENANT_ID=your-azure-tenant-id
MICROSOFT_CLIENT_ID=your-app-client-id
MICROSOFT_CLIENT_SECRET=your-app-client-secret
MICROSOFT_MAILBOX=hr@example.com
```

For Microsoft Graph, create an Azure app registration and grant application permissions for `Mail.ReadWrite` and `Mail.Send`, then give admin consent. Graph mode reads unread Inbox messages, fetches attachments and conversation context, sends threaded replies through Outlook, and marks processed messages as read. IMAP/SMTP values are not required when `MAIL_PROVIDER=microsoft_graph`.

The HR logic is provider-agnostic, so the same recruiter decisions work with Gmail and Outlook. Gmail gets richer thread context through `X-GM-THRID`; Microsoft Graph gets Outlook conversation context through `conversationId`.

Outlook note: Graph replies are sent with Microsoft Graph `createReply`, so applicants see them in the same email thread. In the career mailbox, Outlook may still show the selected incoming message as a single message with a `You replied...` banner. Use Outlook's `View conversation` button, or enable conversation view, to see the sent recruiter reply expanded inside the mailbox thread.

Create tables:

```bash
./venv/bin/python recruiter_agent.py --init-db
```

Process unread emails once:

```bash
./venv/bin/python recruiter_agent.py --run-once
```

Emails classified as not employment/recruitment related are logged and left unread. Recruitment-related emails are marked read after the agent handles them.

Run the recruiter dashboard:

```bash
./venv/bin/python recruiter_dashboard.py
```

Open:

```text
http://127.0.0.1:8090
```

The dashboard shows open requirements, processed candidates, applications, ATS/JD scores, recent email events, and lets you add or close requirements. Each listing row has a detail page so you can inspect the full saved record, including JSON evaluations and raw CV text. Candidate and application records also include a `Download CV` action; new applications download the original attachment, while older records fall back to a text export from the saved CV text.

The AWS `docker-compose.yml` is recruiter-agent-only, so dashboard and local PostgreSQL services are commented out there. To run the dashboard locally, use the Python command above or temporarily restore those commented Compose services.

Previous local Docker dashboard command, if you re-enable those services:

```bash
docker compose up -d recruiter-postgres recruiter-dashboard
```

Open:

```text
http://127.0.0.1:8090
```

Run the always-on recruiter worker in Docker:

```bash
docker compose up -d --build recruiter-agent
docker compose logs -f recruiter-agent
```

The Docker service uses the same `.env` mail, LLM, and database provider settings. For AWS, use an external database in `.env`, for example `DB_PROVIDER=mssql` with `MSSQL_CONNECTION_STRING`, or `DB_PROVIDER=postgres` with an external `DATABASE_URL`.

On AWS, the recruiter container listens on port `8081` for Microsoft Graph webhook calls. Point `GRAPH_NOTIFICATION_URL` to your public HTTPS URL:

```bash
GRAPH_NOTIFICATION_URL=https://your-domain.com/graph/outlook
```

## AWS EC2 Deployment

This deployment runs only the recruiter agent container. The included `docker-compose.yml` has one active service: `recruiter-agent`. Dashboard, local PostgreSQL, and ngrok are commented out.

Before pushing to Git, keep secrets out of the repo:

```bash
cp .env.example .env
# Fill real values only in .env.
# .env, credentials/, documents/, venv/, and local DB files are ignored by .gitignore.
```

AWS prerequisites:

- An EC2 Ubuntu instance with Docker and Docker Compose plugin installed.
- Security group inbound rules for `80` and `443`.
- A domain/subdomain pointing to the EC2 public IP.
- An external database reachable from EC2, such as your SQL Server or managed PostgreSQL.
- Azure app registration for Microsoft Graph with admin consent for `Mail.ReadWrite`, `Mail.Send`, and `Subscriptions.ReadWrite.All`.

Install Docker on a fresh Ubuntu EC2 instance:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in after adding your user to the Docker group.

Deploy the repo:

```bash
git clone your-repo-url company-policy-agent
cd company-policy-agent
cp .env.example .env
nano .env
```

Minimum AWS `.env` values for Outlook/Microsoft Graph:

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-deepseek-api-key

LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=AI Recruiter Agent
LANGCHAIN_API_KEY=your-langsmith-api-key

DB_PROVIDER=mssql
MSSQL_CONNECTION_STRING=Server=your-sql-server; Database=your-db; User Id=your-user; Password=your-password; Encrypt=True; TrustServerCertificate=True; MultipleActiveResultSets=True;

MAIL_PROVIDER=microsoft_graph
MICROSOFT_TENANT_ID=your-azure-tenant-id
MICROSOFT_CLIENT_ID=your-azure-app-client-id
MICROSOFT_CLIENT_SECRET=your-azure-app-client-secret
MICROSOFT_MAILBOX=hr@example.com

GRAPH_WEBHOOK_HOST=0.0.0.0
GRAPH_WEBHOOK_PORT=8081
GRAPH_WEBHOOK_PATH=/graph/outlook
GRAPH_NOTIFICATION_URL=https://your-domain.com/graph/outlook
GRAPH_CLIENT_STATE=use-a-long-random-secret
GRAPH_SUBSCRIPTION_HOURS=48

CV_UPLOAD_API_URL_TEMPLATE=http://smarthrai-apis.runasp.net/api/AiData/applications/{application_id}/cv
CV_UPLOAD_TIMEOUT_SECONDS=30

RECRUITER_APPEND_SIGNATURE=true
RECRUITER_SIGNATURE_SIGNOFF=Regards,
RECRUITER_SIGNATURE_NAME=HR Team
RECRUITER_SIGNATURE_COMPANY=Virtual Admins
RECRUITER_SIGNATURE_COMPANY_URL=https://virtualadmins.org
RECRUITER_SIGNATURE_EMAIL=career@virtualadmins.org
RECRUITER_SIGNATURE_LOGO_URL=https://virtualadmins.org/assets/images/logos/vaadmin-logo.png
```

When a CV is received, the agent first inserts the `recruiter_applications` row, then uploads the original CV file to `CV_UPLOAD_API_URL_TEMPLATE`. The `{application_id}` placeholder is replaced with the new application id. The API response field `attachmentFilename` is saved back into `recruiter_applications.attachment_filename`.

Microsoft Graph does not always apply the Outlook web auto-signature to API-created replies. The agent preserves the draft signature if Graph returns one; otherwise it appends the configured fallback signature above the quoted email thread. Set `RECRUITER_APPEND_SIGNATURE=false` only if your Graph drafts already include the mailbox signature.

Build and start the recruiter agent:

```bash
docker compose up -d --build recruiter-agent
docker compose ps
docker compose logs -f recruiter-agent
```

Create/update database tables:

```bash
docker compose run --rm recruiter-agent python recruiter_agent.py --init-db
```

The container listens on `http://127.0.0.1:8081` inside the EC2 host. Microsoft Graph requires public HTTPS, so put Nginx and Certbot in front of it.

Install Nginx and Certbot:

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

Create an Nginx site:

```bash
sudo nano /etc/nginx/sites-available/recruiter-agent
```

Paste this, replacing `your-domain.com`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location /graph/outlook {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /health {
        proxy_pass http://127.0.0.1:8081/health;
    }
}
```

Enable the site and issue SSL:

```bash
sudo ln -s /etc/nginx/sites-available/recruiter-agent /etc/nginx/sites-enabled/recruiter-agent
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.com
```

Verify the public health endpoint:

```bash
curl -i https://your-domain.com/health
```

Register the Microsoft Graph incoming-mail subscription:

```bash
docker compose run --rm recruiter-agent python recruiter_agent.py --reset-graph-subscription
```

You should see Microsoft validation requests in the logs:

```bash
docker compose logs -f recruiter-agent
```

Microsoft Graph subscriptions expire. Renew daily with cron:

```bash
crontab -e
```

Add this line, replacing the project path:

```cron
0 2 * * * cd /home/ubuntu/company-policy-agent && docker compose run --rm recruiter-agent python recruiter_agent.py --reset-graph-subscription >> graph-subscription-renew.log 2>&1
```

Useful AWS operations:

```bash
docker compose ps
docker compose logs -f recruiter-agent
docker compose restart recruiter-agent
docker compose pull
docker compose up -d --build recruiter-agent
docker compose run --rm recruiter-agent python recruiter_agent.py --list-graph-subscriptions
```

After every new deployment, check:

```bash
curl -i http://127.0.0.1:8081/health
curl -i https://your-domain.com/health
docker compose logs --tail=100 recruiter-agent
```

Trigger from Gmail incoming mail, without polling:

```bash
./venv/bin/python recruiter_agent.py --serve-gmail-webhook
```

This starts a webhook at:

```bash
http://localhost:8080/gmail/push
```

For Gmail to trigger it, configure Gmail API push notifications:

1. Create a Google Cloud Pub/Sub topic.
2. Grant publish access on that topic to `gmail-api-push@system.gserviceaccount.com`.
3. Create a Pub/Sub push subscription pointing to your public HTTPS webhook URL.
4. Set this in `.env`:

```bash
GMAIL_PUBSUB_TOPIC=projects/your-project-id/topics/your-topic
```

5. Save your Gmail OAuth client JSON at `credentials/gmail_oauth_client.json`.
6. Register the Gmail watch:

```bash
./venv/bin/python recruiter_agent.py --register-gmail-watch
```

Gmail watches expire, so renew the watch at least once every 7 days. Google recommends daily renewal.

Localhost cannot receive Google Pub/Sub push directly. For local testing, expose it with a public HTTPS tunnel such as ngrok or deploy this app on a server with HTTPS.

Trigger from Outlook incoming mail, without polling:

```bash
MAIL_PROVIDER=microsoft_graph
GRAPH_WEBHOOK_HOST=0.0.0.0
GRAPH_WEBHOOK_PORT=8081
GRAPH_WEBHOOK_PATH=/graph/outlook
GRAPH_NOTIFICATION_URL=https://your-public-domain.com/graph/outlook
GRAPH_CLIENT_STATE=use-a-random-secret
GRAPH_SUBSCRIPTION_HOURS=48
NGROK_API_URL=http://127.0.0.1:4040/api/tunnels
```

Start the webhook listener:

```bash
./venv/bin/python recruiter_agent.py --serve-graph-webhook
```

In Microsoft Graph webhook mode, each incoming notification is processed by its specific Outlook message id. It does not scan all unread mailbox messages. The `--run-once` and `--watch` commands are the bulk unread-processing modes.

Expose that listener with ngrok in another terminal:

```bash
ngrok http 8081
```

Then let the agent read the ngrok HTTPS URL and register the Microsoft Graph subscription:

```bash
./venv/bin/python recruiter_agent.py --register-graph-ngrok
```

If you see `Ignoring Microsoft Graph notification with invalid clientState`, remove old Outlook subscriptions and register a fresh one for the current `.env` secret:

```bash
./venv/bin/python recruiter_agent.py --reset-graph-ngrok
```

To inspect or delete subscriptions manually:

```bash
./venv/bin/python recruiter_agent.py --list-graph-subscriptions
./venv/bin/python recruiter_agent.py --delete-graph-subscription subscription-id
```

If you already have a fixed public HTTPS URL, set `GRAPH_NOTIFICATION_URL` and register directly:

```bash
./venv/bin/python recruiter_agent.py --register-graph-subscription
```

Microsoft Graph subscriptions expire, so renew the subscription before the printed `expirationDateTime`. The Azure app registration needs Microsoft Graph application permissions for mail processing, plus permission to create subscriptions, such as `Mail.ReadWrite`, `Mail.Send`, and `Subscriptions.ReadWrite.All`, with admin consent.

Docker webhook service:

```bash
docker compose up -d --build recruiter-agent
```

Docker webhook with ngrok, only if you re-enable the commented ngrok service for local testing:

```bash
# Put your ngrok auth token in .env first:
# NGROK_AUTHTOKEN=your-ngrok-token
docker compose up -d ngrok
./venv/bin/python recruiter_agent.py --register-graph-ngrok
```

Open the ngrok inspector at `http://localhost:4040` if you want to see the active public URL and incoming Microsoft validation requests.

Polling fallback is still available:

```bash
./venv/bin/python recruiter_agent.py --watch
```

Run it as a background service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/recruiter-agent.service ~/.config/systemd/user/recruiter-agent.service
systemctl --user daemon-reload
systemctl --user enable --now recruiter-agent
```

Check logs:

```bash
journalctl --user -u recruiter-agent -f
```

The main requirement table is `recruitment_requirements`. Add rows there with `position_title`, experience range, budget range, `job_description`, `urgently_required`, `needed_within_days`, and `status='open'`.

Seed sample requirements:

```bash
./venv/bin/python scripts/seed_recruitment_requirements.py
```

This adds or updates:

- Python Developer
- Accountant

If Docker says `permission denied while trying to connect to the Docker daemon socket`, run:

```bash
sudo docker compose up -d --build recruiter-agent
./venv/bin/python recruiter_agent.py --init-db
```

For a permanent fix, add your Linux user to the Docker group and open a new terminal:

```bash
sudo usermod -aG docker "$USER"
```
