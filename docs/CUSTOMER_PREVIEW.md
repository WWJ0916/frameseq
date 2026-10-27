# Customer and invited administrator preview

The optional gateway exposes projects, episode/shot planning, storyboards, version selection, episode exports, clip composition, local video generation and private history. Temporal AI repainting is not implemented.

Private configuration `daily_limit` is an integer 1–100 (default 2). This deployment used 10 daily submissions with one pending task. Existing and uncertain submissions remain counted; increasing the limit never clears usage. Authenticated `/api/experience` returns the caller's limits and expiry.

Creators require `allowed_user_ids` and login-name invitation. Administrators are denied by default; an operator may explicitly configure `allowed_admin_ids` and `allowed_admin_usernames` for a separate admin account. This does not promote a customer or expose every administrator. Administrative pages, assets and operations use exact route/method allowlists. Every request revalidates the upstream session; unsafe requests retain Origin and CSRF checks. Cloud model submission, registration, invitation creation, external language providers and arbitrary forwarding remain blocked even for invited admins.

`python -m scripts.create_admin_trial` creates a separate administrator idempotently with generated credentials stored only under private `.secrets`. Existing usernames are never promoted or reset. The account itself has no automatic deletion; gateway `expires_at` closes public access to both roles.

Validation: 26 targeted CPU tests passed (15.414 s), including real API/database owner isolation and six invited-admin transport tests. Deployed local HTTP and US-proxied public HTTPS verified existing creator and separate admin passwords, customer pages, administrative statistics, ownership denial, CSRF rejection, invalid-write validation and video Range206. No model job or paid request was submitted by these feature checks. Browser interactions and the customer's own network remain unverified.

A stopped Quick Tunnel cannot promise its previous random hostname. This instance needed a replacement URL and preserved account credentials; expiry was extended by two days without quota reset. This is temporary process-dependent access, not persistent hosting or a production security/concurrency certification.
