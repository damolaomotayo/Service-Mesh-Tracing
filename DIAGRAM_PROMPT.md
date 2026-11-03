# Diagram prompt (copy everything below into an image-generation model)

Create a clean, professional cloud infrastructure diagram, 16:9 landscape, white background, flat vector style, large readable labels, thin dark-grey lines, soft pastel fills. Title at the top: "Service Mesh and Tracing: sidecar proxies on one EC2 instance".

LAYOUT, left to right.

LEFT: a "Browser (your laptop)" box with two items: "http://<EC2-PUBLIC-IP>:8080/ (app)" and "http://<EC2-PUBLIC-IP>:16686/ (Jaeger traces)". Solid arrows labeled "HTTP 8080" and "HTTP 16686" go to the right. Below it a small "Terminal (SSH port 22)" box with a dashed arrow to the right.

MIDDLE: a thin firewall icon labeled "Security group: inbound SSH 22, TCP 8080 and TCP 16686, all from my IP only; outbound all".

RIGHT: a large AWS-orange-bordered box labeled "AWS Region > Default VPC > EC2 instance m7i-flex.large (Ubuntu 24.04, 2 vCPU, 8 GiB)". Inside it, one big blue rounded box labeled "Docker Engine". Inside the Docker box, draw four "pods", each a rounded rectangle containing two boxes side by side: a white box for the app and a green box labeled "sidecar proxy (Traefik)". Label the pods:
1. "frontend pod" (app "frontend", sidecar has the public port 8080).
2. "catalog-v1 pod" and "catalog-v2 pod" stacked (apps "catalog v1" and "catalog v2").
3. "pricing pod" (app "pricing").
Inside each pod draw a tiny arrow between app and sidecar labeled "127.0.0.1 (plain HTTP)".
Between the sidecars draw thick green arrows labeled "mTLS (certificates from the mesh CA)": frontend sidecar to both catalog sidecars, and each catalog sidecar to the pricing sidecar. On the arrows from frontend to catalog-v1 and catalog-v2 add the labels "canary split 90% / 10%" and "header x-canary: true goes to v2".
At the top of the Docker box a small yellow box "Mesh rules: mesh/traffic.yaml" with a dashed arrow "rendered live to every sidecar". Next to it a small key icon box "Mesh CA and certificates".
At the bottom a purple box "Jaeger (trace UI :16686, collector :4318)" with dashed purple arrows from every sidecar labeled "OTLP spans".

Numbered markers on the main path:
1. Browser to frontend sidecar: "request".
2. Frontend app to its sidecar: "call catalog".
3. Sidecar to catalog sidecar: "mTLS, retries, 3 s timeout".
4. Catalog sidecar to pricing sidecar: "mTLS, circuit breaker, 1 s timeout".
5. All sidecars to Jaeger: "one trace id across all hops".
Also show a red cross on a "catalog-v2 sidecar down" icon with a green arrow rerouting to v1 labeled "retry on the healthy version".

Add a small legend: blue = public entry point, green = sidecar proxies and mTLS, purple = tracing, yellow = rules. Add a footer note: "Everything runs as containers on this one instance. No other AWS services are used."

Do not add any AWS services (no App Mesh, X-Ray, ALB, IAM). Use generic icons, with a simple Docker whale as the only logo. Make sure all text is spelled exactly as written above.
