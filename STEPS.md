# STEPS: Service Mesh and Tracing

## Prerequisites (set these up BEFORE step 1)

| Need | Details |
|---|---|
| **AWS account** | Any account that can launch EC2. No IAM role, no other AWS service. Use one region for everything below. |
| **Instance type** | `m7i-flex.large` (2 vCPU, 8 GiB). This project needs about 1 GiB. |
| **OS image (AMI)** | Ubuntu Server 24.04 LTS (64-bit x86). 22.04 also works. |
| **Disk** | 20 GiB gp3. |
| **Key pair** | An RSA `.pem` key pair created in EC2 (step 0a). |
| **Security group** | Created in step 0b. **Inbound: exactly three rules: (1) SSH, TCP port 22; (2) Custom TCP, port 8080 (the app); (3) Custom TCP, port 16686 (the Jaeger trace UI). All with source My IP.** **Outbound: leave the default (All traffic, 0.0.0.0/0)**, because the instance must reach Docker Hub (the Traefik, Jaeger and Python images) and Docker's apt repo. |
| **Code on the instance** | Option A: private GitHub repo plus a fine-grained token (Contents: Read) used as the password for `git clone`. Option B: copy the zip with `scp` (step 2). |
| **Accounts / tokens** | None. The mesh's certificates are generated on the instance (git-ignored). |
| **On your laptop** | A terminal with `ssh`/`scp` and a web browser. **No SSH tunnel is needed:** open `http://<PUBLIC-IP>:8080/` and `:16686/` directly. |

**Time:** about 10 minutes of setup (the first run downloads three images), 6 minutes for the full demo. **Cost:** about $0.10/hour at your quoted rate, plus pennies for the disk. Do step 12 when finished.

---

## Step 0: Create the key pair, security group and instance (AWS console)

**0a. Key pair.** EC2 > Network & Security > Key pairs > **Create key pair**. Name `mesh-lab-key`, type **RSA**, format **.pem**. The file downloads once; keep it safe. On your laptop, lock down its permissions:

```bash
# Linux / macOS
chmod 400 ~/Downloads/mesh-lab-key.pem
```
```powershell
# Windows PowerShell (OpenSSH)
icacls "$HOME\Downloads\mesh-lab-key.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

**0b. Security group.** EC2 > Network & Security > Security Groups > **Create security group**.
- Name: `mesh-lab-sg`, Description: `SSH, app and Jaeger from my IP`, VPC: the default VPC.
- **Inbound rules > Add rule** (do it three times):
  1. Type `SSH`, Protocol `TCP`, Port range `22`, Source `My IP`
  2. Type `Custom TCP`, Protocol `TCP`, Port range `8080`, Source `My IP`
  3. Type `Custom TCP`, Protocol `TCP`, Port range `16686`, Source `My IP`
- **Outbound rules:** leave the default rule (Type `All traffic`, Destination `0.0.0.0/0`).
- Click **Create security group**.

> Showing the demo to someone on a different network? Their browser is blocked by "My IP". Either share your screen, or add their public IP followed by `/32` as the source of the 8080 and 16686 rules. Do not use `0.0.0.0/0`.

**0c. Launch the instance.** EC2 > Instances > **Launch instances**.
- Name: `mesh-lab-box`
- Application and OS Image: **Ubuntu**, choose **Ubuntu Server 24.04 LTS**, architecture **64-bit (x86)**
- Instance type: `m7i-flex.large`
- Key pair: `mesh-lab-key`
- Network settings > **Edit**: Auto-assign public IP **Enable**; Firewall: **Select existing security group** > `mesh-lab-sg`
- Configure storage: **20 GiB**, `gp3`
- Advanced details > IAM instance profile: leave as **No IAM role**
- Click **Launch instance**, wait until Instance state is `Running` and Status check is `2/2 passed`, then copy the **Public IPv4 address**. This is your `<PUBLIC-IP>` below.

> If your network changes later, SSH and the pages will time out because "My IP" no longer matches. Edit **all three** inbound rules of `mesh-lab-sg` and set Source to My IP again. Stopping and starting the instance also gives it a new public IP.

## Step 1: Connect to the instance (from your laptop)

```bash
ssh -i ~/Downloads/mesh-lab-key.pem ubuntu@<PUBLIC-IP>
```
Type `yes` at the fingerprint prompt. Every command from here to step 11 runs **on the instance**.

## Step 2: Update packages and get the code

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git unzip
```

**Option A (private GitHub repo):**
```bash
git clone https://github.com/<you>/service-mesh-tracing.git
cd service-mesh-tracing
```
When asked, username = your GitHub username, password = the fine-grained token.

**Option B (no GitHub), run from your laptop in the folder holding the zip, then continue on the instance:**
```bash
# on your laptop
scp -i ~/Downloads/mesh-lab-key.pem service-mesh-tracing.zip ubuntu@<PUBLIC-IP>:~
# on the instance
unzip service-mesh-tracing.zip
cd service-mesh-tracing
```

## Step 3: Make the scripts executable (do not skip)

Files that were zipped, uploaded to GitHub or copied between systems often lose their "executable" flag. Without this you get `Permission denied`.

```bash
chmod +x scripts/*.sh
ls -l scripts/        # every .sh line should start with -rwxr-xr-x
```

## Step 4: Install Docker

```bash
./scripts/setup.sh
```
It runs `apt update`/`upgrade`, installs `ca-certificates curl git gnupg openssl python3 python3-yaml unzip`, installs Docker Engine and Compose v2 from Docker's official repo (falling back to Ubuntu's `docker.io` package if that repo is unreachable), starts Docker and prints the versions. Takes 3-5 minutes. It ends with `Setup complete.` Neither Traefik nor Jaeger is installed on the instance: both run as containers.

## Step 5: Unit tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
Ends with `OK` (a few seconds). They test the config generator, the certificate script, the traffic-rule editor and the scripts. The long end-to-end test (`test_mesh_integration`) needs a `traefik` program on the instance itself, so it shows as `skipped`; that is normal. It was run for real, with Traefik, before this zip was made.

## Step 6: Preflight (run this well before the demo)

```bash
./scripts/preflight.sh
```
Checks Docker, openssl, python3-yaml, disk (6 GB), memory (3.5 GB) and ports 8080, 16686 and 15101; checks that Docker Hub is reachable; pulls Traefik, Jaeger and Python; **starts the whole mesh** (creates certificates, renders sidecar configs, builds the app image, starts 9 containers), runs the 12 smoke checks and the tests. Every line must say `ok`, and the last line is `Preflight passed. The mesh is running.` First run takes 4-8 minutes.

## Step 7: Start the mesh

```bash
./scripts/up.sh
```
Creates the mesh certificate authority and one certificate per pod, writes each sidecar's config, builds the app image, starts the 9 containers and waits until a request travels frontend, catalog and pricing. Takes 1-2 minutes (a no-op if preflight left it running). It prints:

```
App    : http://<PUBLIC-IP>:8080/
Traces : http://<PUBLIC-IP>:16686/
```

## Step 8: Health check

```bash
./scripts/smoke.sh
```
Every line says `ok` and it ends with **`All 12 checks passed.`**

## Step 9: Open the pages in your browser (on your laptop)

- `http://<PUBLIC-IP>:8080/`: a page saying `frontend > catalog v1 (catalog-v1) > pricing: ok`. After a canary change (step 11) refresh and watch `v2` appear.
- `http://<PUBLIC-IP>:16686/`: Jaeger. Service `frontend-sidecar` > **Find Traces** > click the newest trace: one request across the frontend, catalog and pricing sidecars.

## Step 10: Run the demo (on the instance)

```bash
./scripts/demo.sh all           # all seven scenarios, about 6 minutes
```
Or one at a time (each starts from the same known state: all traffic on v1, 3 retry attempts, nothing broken):
```bash
./scripts/demo.sh mtls       # an intruder without a certificate is refused; the frontend's certificate is accepted
./scripts/demo.sh canary     # v1 100% > 90/10 > 50/50 > v2 100%, live, no deploy
./scripts/demo.sh header     # x-canary: true reaches v2 while normal users stay on v1
./scripts/demo.sh failover   # v2's sidecar dies; retries hide it; with retries off, users see errors
./scripts/demo.sh timeout    # pricing takes 3 s; the mesh cuts it off at 1 s and the page degrades
./scripts/demo.sh breaker    # pricing returns 500s; the circuit opens and shields it; it recovers by itself
./scripts/demo.sh trace      # one request, one trace, three services, in Jaeger
```
Each scenario ends with a green `PASS`. Keep the app page open in one tab and Jaeger in another.

## Step 11: Useful commands (a manual walkthrough you can do live)

```bash
./scripts/mesh.sh status                  # the live rules (mesh/traffic.yaml)
./scripts/mesh.sh canary 90 10            # 90% of traffic to v1, 10% to v2, applied live
./scripts/mesh.sh sample 100              # send 100 requests and count which version answered
./scripts/mesh.sh sample 20 x-canary:true # same, with the tester header (goes to v2)
./scripts/mesh.sh canary 100 0            # back to v1 only
./scripts/mesh.sh retries 1               # turn retries off (default 3)
./scripts/chaos.sh pricing delay_ms=3000  # make pricing slow
./scripts/chaos.sh pricing error_rate=1 status=500    # make pricing fail
./scripts/chaos.sh all clear              # heal everything
sudo docker stop mesh-catalog-v2-sidecar  # kill a sidecar (start it again with: sudo docker start mesh-catalog-v2-sidecar)
sudo docker logs --tail 20 mesh-frontend-sidecar      # what a sidecar says
cat mesh/generated/frontend/dynamic/mesh.yml          # the config the control plane wrote for the frontend's sidecar
```

## Step 12: Clean up, then terminate the instance

```bash
./scripts/destroy.sh
```
Then in the AWS console: EC2 > Instances > select `mesh-lab-box` > **Instance state > Terminate instance**. Optionally delete the `mesh-lab-sg` security group and `mesh-lab-key` key pair too.

---

## What you should see

| Step | Result |
|---|---|
| 5 | Tests end with `OK` (the integration test skipped) |
| 6 | All `ok`, then `Preflight passed.` |
| 7 | Two URLs containing your public IP |
| 8 | `All 12 checks passed.` |
| 9 | The page shows `catalog v1`; Jaeger lists traces for `frontend-sidecar` |
| 10 | Seven `PASS` lines, then `All scenarios passed.` |

## Risky dependencies and fallbacks

| Dependency | Risk | Fallback |
|---|---|---|
| Docker Hub (`traefik`, `jaegertracing/all-in-one`, `python`) | Rate limit or a withdrawn tag | `sudo docker login` with a free account, or change the tag in `.env` and re-run preflight |
| Image tags in `.env.example` (`traefik:v3.7.17`, `all-in-one:1.62.0`, written from memory) | A tag is not found | Pick another tag of the same image on Docker Hub and put it in `.env`. Traefik v3 is expected; the config was tested with 3.7.17 |
| Sidecar config reload | A change is not picked up on a bind-mounted folder | The scripts wait, then restart the sidecars once automatically. By hand: `sudo docker restart mesh-frontend-sidecar` |
| Jaeger takes a few seconds to show spans | `demo.sh trace` or smoke check 12 waits up to 25 s | Wait and retry; check `sudo docker logs mesh-jaeger` |
| Timing-based demo steps (canary percentages, retries, breaker) | A busy instance can shift counts | The checks use ranges; re-run a scenario on its own |
| Do not restart an *app* container alone | Its sidecar shares its network and would lose it | Run `./scripts/up.sh` (recreates both) |
| MinIO, LocalStack | **Not used** | none needed |

## If something goes wrong

| Symptom | Fix |
|---|---|
| SSH: `Connection timed out` | Your IP changed. Edit security group `mesh-lab-sg`, inbound rule SSH port 22, Source My IP. Also confirm the instance is Running and has a public IP. |
| Browser: the app never loads at `http://<PUBLIC-IP>:8080/` | (1) Use `http://` not `https://`, and include `:8080`. (2) The inbound rule for 8080 must exist with Source My IP. (3) Check the IP is the instance's current public IP. (4) On the instance: `curl -i http://localhost:8080/api/products` should return `200`. If it works, the problem is the security group; if not, run `./scripts/up.sh`. |
| Jaeger does not load at `:16686` | Same checks, with the 16686 rule. On the instance: `curl -I http://localhost:16686/` should return `200`. |
| Jaeger has no `frontend-sidecar` service | Send a request first (`curl localhost:8080/api/products`), wait 10 seconds, reload the page. |
| Testing the public IP from the instance itself hangs | Expected: the security group only allows your laptop's IP. Use `localhost` on the instance. |
| SSH: `Permission denied (publickey)` | Use user `ubuntu` (not `ec2-user`), the right `.pem`, and `chmod 400` on it. |
| SSH: `UNPROTECTED PRIVATE KEY FILE` | `chmod 400 mesh-lab-key.pem` |
| `./scripts/x.sh: Permission denied` | `chmod +x scripts/*.sh` (step 3). Or run with `bash scripts/x.sh`. |
| `bad interpreter: /usr/bin/env bash^M` | Windows line endings. Run `sed -i 's/\r$//' scripts/*.sh` |
| `permission denied` on the Docker socket in your own commands | The scripts use `sudo` for you. For manual commands use `sudo docker ...`, or log out and back in once. |
| Preflight: `python3-yaml installed` fails | `sudo apt install -y python3-yaml` (setup.sh does this) |
| Preflight: `Docker Hub reachable` or a `Pull ...` line fails | Check the outbound rule is All traffic; `sudo docker login` with a free account; if the tag is not found, edit the image in `.env`. |
| Preflight: `Port 8080 is free` (or 16686 or 15101) fails | `sudo ss -ltnp \| grep -E ':(8080\|16686\|15101) '`, stop what uses it, or `./scripts/destroy.sh` if it is an old run of this project. |
| Preflight: `The mesh starts and requests flow` fails | `sudo docker compose ps` (9 containers Up?); `sudo docker logs --tail 30 mesh-frontend-sidecar` shows config errors; `sudo docker logs --tail 30 mesh-catalog-v1` shows app errors. |
| A sidecar says `CERTIFICATE_VERIFY_FAILED` / `bad certificate` | Old certificates (they last 30 days): `./scripts/destroy.sh --keep-env` then `./scripts/up.sh`. |
| Smoke: `mTLS: a client with a mesh certificate gets in` fails | `ls certs/` should show `ca.pem` and `frontend.pem`; check that `curl` is installed; check `sudo docker ps` shows `mesh-catalog-v1` publishing `127.0.0.1:15101`. |
| Smoke: `100/0 split sends everything to v1` fails | Rules left over from a half-finished demo: `./scripts/mesh.sh canary 100 0` and `./scripts/mesh.sh retries 3`, `./scripts/chaos.sh all clear`, and start any stopped sidecar with `sudo docker start mesh-catalog-v2-sidecar`. |
| `demo.sh`: `The mesh is not running` | `./scripts/up.sh` |
| `demo.sh`: `could not reset the mesh` | Run `./scripts/smoke.sh` to see which check fails; `sudo docker ps -a` shows stopped containers; `./scripts/up.sh` recreates everything. |
| Disk full | `sudo docker system prune -af` (this removes cached images; preflight pulls them again) |
