# H3 Turbo8 operator integration

The optional self-hosted H3 contract uses 8 steps, Euler/simple, CFG1, 864x480, 124 frames/24fps, FL2VA Turbo8 v1.0 at strength1, video shift12/audio shift3. All creation paths use the shared server parameter factory. Previous task records retain their original parameters; reusing a prompt creates a new request under the current profile. Other model families keep their own compatible inference contracts.

Operators must provision the matching reviewed dual runner, matching LoRA/runtime metadata and a genuine profile-specific verification record. Old 20-step records do not establish readiness for this profile. Both rank reports must prove matching steps/sampler/shifts, LoRA checksum, strength, exactly-once application and completed sampling before decoding. No general-purpose H3 installer or new-host Docker validation is included by this change.

23 targeted CPU tests and 20 frontend tests passed. The deployed customer loopback verified login, configured model catalog and creation/project/storyboard assets. The subsequent US-proxied public TLS recheck timed out; browser interaction and a fresh deployed HTTP-to-video run were not repeated. A prior isolated full Turbo8 video passed all-frame decode/SHA/three-frame review, with a prompt-color deviation, and is not a quality-equivalence claim.

The verified experiment used software thermal pacing at 79C pause / 78C resume and an unchanged 82C emergency stop. Its approximately40-minute wall time included approximately32 minutes of cooling. This is a hardware-specific observation, not a generic Turbo performance promise. Shared-server fans/power were not changed. A user must evaluate ventilation/cooling and any administrator-approved power limit independently.

Upstream: https://github.com/ModelTC/Minimax-H3-Turbo/blob/main/COMFYUI_SETUP_AND_INFERENCE.md
