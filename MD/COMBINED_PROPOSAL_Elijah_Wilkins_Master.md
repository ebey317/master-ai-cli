# ELIJAH WILKINS — MASTER PROPOSAL TO ANTHROPIC

**Prepared for:** Partnerships + Business Development  
**Date:** May 25, 2026  
**From:** Elijah W. Sr., Indianapolis, IN  
**Contact:** ebey317@gmail.com | github.com/ebey317

---

## EXECUTIVE SUMMARY — Three Asks, One Ecosystem

I am an HVAC technician in Indianapolis who has spent the last year building a distributed AI network on top of Anthropic's Claude and the Model Context Protocol (MCP). I'm writing because I have three related partnership opportunities to present:

1. **Business Partnership** — A portfolio of revenue-generating services targeting underserved communities
2. **Technical Partnership** — Claude Partner Network membership + Certified Architect credential + co-selling support
3. **Market Development Partnership** — Infrastructure to reach communities your current marketing doesn't reach

All three point to the same problem: frontier AI is concentrating value among people who already have resources. I'm building the system to redirect that value toward people who need it.

---

## PART ONE: THE DISTRIBUTED AI WORK NETWORK

### The Problem

The AI economy is producing enormous value. Almost none of it reaches the communities that need it most.

- Large consulting firms train 30,000 professionals on Claude at a time
- Enterprise integrators land seven-figure deals
- The infrastructure, training, certification, and co-selling all flow to organizations that already have capital and credibility

Meanwhile:
- The single mother in government housing keeping the lights on is unaddressed
- The kid in a cold house with no entertainment has no on-ramp to the digital economy
- The man who would rather find productive work than nothing at all needs a system, not a pamphlet

### The Solution: A Five-Phase Distributed Network

**Phase 1 — Founder-Led Pilot (Current)**
- Build the full MCP stack (done)
- Define service offerings (defined)
- Land first clients (in motion)
- Document every step
- One to three trusted collaborators help complete tasks
- **Goal:** Prove money comes in before infrastructure is shared

**Phase 2 — Distributed Operator Network**
- Operators join with their own machines and MCP servers
- Shared task board, intake templates, quality standards
- Single client contract routes to multiple operators:
  - One handles document processing
  - One handles research
  - One handles deliverables
- **Result:** Increased throughput without proportional overhead

**Phase 3 — Business Fund**
- Revenue split on every job:
  - 70% to the operator who did the work
  - 20% reinvested into shared infrastructure (tools, subscriptions, hardware, legal)
  - 10% held in reserve
- No forced buy-ins. Contribution comes from completed work, not upfront capital

**Phase 4 — Registered Business**
- LLC formation
- Business bank account
- Tax structure
- Business-owned Claude Team or Enterprise workspace
- Shared projects
- Professional client presentation
- Operators now have institutional backing without gatekeeping

**Phase 5 — Central Workstation**
- Business purchases a central server with remote access and shared storage
- Operators connect from wherever they are
- Centralized compute power + distributed human skill = real agency

### Technical Architecture

The stack runs on Claude as the primary AI layer. MCP is the integration backbone.

**Current Founder Stack:**
- Custom multi-provider AI routing (Groq, Fireworks, OpenRouter, Gemini, local Ollama fallback)
- Browser automation agent (Pupil Chrome extension)
- Voice-to-text server
- Claude Code integration with custom hooks (retry policy, session management)

**This is not a whiteboard diagram — it runs today on a home workstation in Indianapolis running Ubuntu 24.04.**

**Network Design Principles:**
- Resilient without requiring paid API keys at operator level
- Operators without subscriptions run local models as fallback
- Operators with Claude use it as primary intelligence
- Architecture handles both
- MCP donation to Linux Foundation makes this vendor-neutral at protocol layer
- Claude integration is a deliberate choice, not lock-in

---

## PART TWO: THE REVENUE SERVICES PORTFOLIO

The network feeds on real client work. Here are the six revenue engines:

### 1. REENTRY DESK — Document Assistance for Crisis Recovery

**STATUS: BUILT & DEPLOYED**

**Who:** People coming out of incarceration or housing crisis  
**What:** Pre-filled government forms and intake documents  
**Services:**
- Housing intake forms
- SNAP / benefits enrollment
- ID replacement paperwork
- Medicaid / SSI applications
- **5 free fair-chance job applications included** (hook)

**Revenue Model:**
- Individual: Tiered pricing ($2 per job application to $50/month subscription)
- B2B: Shelter/nonprofit contracts ($300-500/month for unlimited client fills)

**First Client:** Warm relationship at Indianapolis shelter (proven contact)

**Grant Target:** Indiana Second Chance Act funding ($10K to $70K)

**Infrastructure:** MCP reentry-desk server (6 tools: create_client, get_client, list_forms, fill_form, mark_complete, get_status) + Pupil browser automation + client_profile.json intake system. **Currently running at ~/projects/reentry-desk/**

**Year 1 Reality:** One shelter contract ($400/month) + grant funding = $40K-80K revenue. First client call scheduled this week.

---

### 2. JOB APPLICATION SERVICE — Automated Fair-Chance Job Filling

**Who:** Anyone looking for work, especially people with barriers to employment  
**What:** Automated job application filling at client-set prices  
**Pricing:**
- Retail: $2 per application (high volume, low margin)
- **Better path — B2B:** Organizations pay $300-500/month for unlimited fills

**Tiered Packages:**
- Free: 5 fair-chance applications included with Reentry Desk
- Basic: 15 applications per month
- Premium: Unlimited applications + custom employer targeting

**How It Works:**
- Client provides profile once (name, work history, skills, address)
- Pupil fills and submits applications automatically
- Confirmation email required to count
- Fair-chance employers prioritized

**Powered by:** Pupil + client_profile.json + form templates

**Year 1 Reality:** 50 retail packages + 3 B2B organizations = $12K-18K/month

---

### 3. RESUME BUILD — SSA Work History Pull + Complete Chronicle

**Who:** Anyone whose employment history is scattered or incomplete  
**What:** Verified employment chronicle from Social Security Administration  
**Services:**
- Pull full earnings and employer history from SSA
- Build detailed 5-year resume versions
- Documented chronicle with dates and titles
- One-time service

**Price:** $75-150 per resume

**Why This Matters:** 
- People with records, gaps, or history across states have unverifiable work history
- Employers demand clean records
- SSA pull gives documented proof of every job
- Nobody else is doing this at street level

**Year 1 Reality:** 20 resumes/month at $75 = $18K-36K/year part-time

---

### 4. EVENT CHILDCARE — Supervised Daytime Care During Major City Events

**Who:** Families at major events (Black Expo, Final Four, Indy 500, etc.)  
**What:** 12-hour supervised entertainment + meals  
**The Offer:**
- Hours: 7am to 11pm
- Venue: Rented cabin or building + direct park access
- Entertainment: Movies, music videos, Xbox Game Pass, Just Dance (deliberately tiring them out)
- Two meals: Lunch (pizza) + Dinner (burgers, chili dogs, nachos, comfort food)
- Staff supervision + safety tape boundary + adult monitoring

**Revenue Model 1 — Per-Event:**
- $75 per child per event
- 40 kids per event = $3,000 gross per event
- 20 events/year = $60K gross

**Revenue Model 2 — Subscription (Better):**
- Families pay $50/month subscription
- Covers unlimited anchor events (Black Expo, Final Four, Indy 500, etc.)
- 150 subscriber families = $90K/year recurring before a single event runs

**Overnight Handoff (Legal):**
- 11pm — kids transfer to licensed daycare partner
- Kids arrive fed, tired, calm, already cared for
- Partner gets guaranteed volume on event weekends in advance
- **Result:** You serve daytime. They serve overnight. Both win.

**Powered by:** MCP event scheduler + handoff manifest + Pupil-generated documents

**Year 1 Reality:** 50 families subscribing + 5 events = $75K-100K

---

### 5. THE ARCADE — 24/7 Adult Gaming Bar (End-Goal Flagship)

**What:** Bring back the arcade. A bar where you play instead of just drink.

**The Experience:**
- 100+ personal screens (looks like a bar, plays like an arcade)
- Bring your own controller or use provided
- Bar service + food
- Xbox corporate Game Pass venue account (real licensing conversation with Microsoft)
- 21+ wristbands for alcohol
- Kids welcome until legal curfew (law enforces the exit, not staff)
- Subscription membership
- **Open 24/7**

**Revenue Model:**
- Monthly membership: $30-50/month
- Per-hour walk-in: $5-10/hour
- Food and beverage: Bar margin
- Proven barcade model: $800K to $1.5M annual revenue per location

**Why This Works:**
- Barcade industry is proven and underserved in most mid-sized cities
- Adults want community gaming spaces
- Subscription memberships are sticky
- Nintendo, Xbox, PlayStation content licences (some games)
- Microsoft is open to venue partnerships (they've done them before)

**Powered by:** MCP managing screens, schedules, memberships, inventory

**Timeline:** Phase 4-5 (after 3-5 years of Phase 1-3 revenue)

**Capital Required:** $300K-700K (from accumulated Phase 1-3 profits or SBA loan)

**Revenue Potential:** $1M+ annually at capacity

---

### 6. THE BUILDING — The North Star Vision

**The Physical Reality:**
- 3-4 story structure made of 2-4 shipping containers in stepped formation
- Built by you, the HVAC technician, yourself (you can do this)

**Floor Breakdown:**
- **Bottom (Widest Base):** The Arcade + rentable business space + public space
- **Middle (Split):** Private work suite for you + luxury VIP rental suite (hot tub, personal theater, appointment-based)
- **Top (Most Private):** Your home. Earned. Private. Nobody else gets up there.
- **Fourth Container (Optional):** Government-partnered winter shelter (free, digital keys, women fleeing domestic situations, people with jobs who need a bridge)

**Revenue Streams:**
- Arcade operations
- Rentable business office space upstairs
- Luxury VIP suite rental (appointment-based, $500-1000/booking)
- Boarding house rooms (when arcade isn't the primary focus)
  - $300/week for bedroom
  - $800/month for studio efficiency
- Government shelter partnership (per-diem funding from HUD/Indiana Housing Authority)

**The Off-Grid Revolution:**
- Solar panels on roof
- Wind turbine(s)
- Power the whole compound independently
- Sell excess energy back to grid
- **Operating costs drop to near zero**
- Every dollar coming in is almost pure margin

**Container Build Cost:** $25K-37K (materials + your labor)

**Result:** 
- $180K-250K annual revenue at capacity
- Zero rent
- Zero mortgage (own outright after build)
- Zero utility bills
- That's not millionaire money — that's **freedom money**

---

## PART THREE: THE BUSINESS MODEL & SCALING STRATEGY

### The 70/20/10 Revenue Split

**From Day One:**
- 70% → Labor payout to whoever did the work
- 20% → Reinvestment (tools, subscriptions, hardware, legal setup)
- 10% → Reserve

Why this works:
- No forced buy-ins
- Everyone sees immediate return on effort
- Reinvestment comes from success, not upfront capital
- Creates genuine incentive alignment

### The Realistic Timeline

**Year 1-2: Proof of Concept**
- Reentry Desk + Job Applications + Resume Builds operating
- $60K-150K combined revenue
- Prove the model works
- Build reputation with first 2-3 clients

**Year 2-3: Scale Operations**
- Event Childcare launches with anchor events
- Distributed operator network begins (Person A joins)
- Combined revenue: $250K-400K
- Start building the business fund

**Year 3-5: Ready for Arcade**
- Multiple operators active
- 3-5 contracts live
- Accumulated capital from reinvestment
- SBA loan application ready
- Launch arcade with investor or loan backing

**Year 5-8: The Building**
- Arcade proves profitability
- Shipping containers purchased and built out
- Living situation transforms
- Net worth crosses $1M

### Immediate First Move (This Week)

Call the shelter contact. Have a listening conversation. Let them tell you the pain. Design the Reentry Desk package around what they actually need. Land the first contract. Prove $3,500-4,000/month is real. Then everything else scales from there.

---

## PART FOUR: WHY ANTHROPIC SPECIFICALLY

I considered other cloud AI providers. I chose to anchor on Anthropic for four reasons:

### 1. Claude's Reasoning Quality
Master AI is an agent stack — it makes plans, executes them, observes outcomes, self-corrects. That requires a brain that can hold context across long sessions and recover from errors gracefully. Claude does this better than any alternative I've tested.

### 2. Anthropic's Safety Posture
My executor framework refuses sensitive fills regardless of operator-friendliness. My audit log has no off-switch. My configs include explicit safety clauses that survive future contributors. I didn't add these because Anthropic told me to — this is how I think the work needs to be done. **We are aligned by disposition, not contract.**

### 3. Anthropic Genuinely Seems to Care Who Uses AI
I read your usage policies and public communications. The framing of "helpful, harmless, honest" is not marketing surface — it shapes how Claude actually behaves in long conversations. I've run hundreds of hours of agentic work through Claude and it stays itself in a way the others don't.

### 4. I'm Already an Anthropic Customer
I run Claude Code as my development environment for building all of this. Anthropic has already been paying me back in productivity what I pay in subscription. **The relationship is real before this letter, not hypothetical.**

---

## PART FIVE: WHAT WE'RE ASKING FOR

### Claude Partner Network Membership
- Access the partner portal
- Participate in co-selling opportunities
- Get introduced to enterprise and nonprofit clients
- Directory listing for services

### Claude Certified Architect Credential
- Validation that I understand the architecture deeply
- Credential I can cite when speaking to organizations about partnerships
- Platform to share best practices from the distributed network

### Co-Selling Support
For nonprofit and community development clients:
- Introductions to organizations serving reentry populations
- Co-marketing for Reentry Desk to shelters, nonprofits, housing authorities
- Technical consultation for large-scale deployments
- Grant writing support (we identify opportunities, you provide testimonials)

### Architecture Review & Technical Partnership
- Introduction to Anthropic's applied AI engineering team
- Code review of the MCP implementation
- Feedback on distributed network design
- Guidance on scaling to Phase 5

### Consideration for Co-Investment
As the network reaches Phase 3 revenue milestones:
- Not asking for seed capital
- **Asking whether Anthropic wants to invest alongside the business growth**
- Revenue milestone triggers (e.g., when we hit $250K annual revenue, discuss co-investment terms)

---

## PART SIX: WHAT ANTHROPIC GETS

### A Real-World Claude Deployment in an Unreached Market
- Trade workers
- People in poverty
- Single-parent households
- Operators with no traditional tech background
- **Communities where AI-as-economic-access lands as a life change, not a product announcement**

### A Case Study for MCP-Native Distributed Work at Community Scale
- Real architecture implemented and proven
- Not a whiteboard diagram
- Not a thought experiment
- **Working system with measurable outcomes**

### A Certified Architect Candidate Who Can Speak Both Languages
- Technical architecture to engineers
- Human stakes to community leaders
- In plain language, to any room
- **From lived experience, not sales training**

### A Distribution Channel Into Communities Anthropic Can't Reach
- I already have relationships with shelters
- I already talk to trades workers in Discord
- I already understand fair-chance hiring
- **I can introduce Anthropic to communities your current marketing doesn't credibly reach**

---

## PART SEVEN: ABOUT THE FOUNDER

I am Elijah W. Sr., age 37, based in Indianapolis, Indiana.

By day: HVAC technician, EPA Type I & II certified, recently fair-chance hired

By night: AI builder

**The Journey:**
- No computer science degree
- No development team
- No paid API key (started with free tier)
- Learned by doing, reading documentation, reverse-engineering production architecture, and treating every failed deployment as a diagnostic session
- Personal principle: "I'm not using the computer. I'm programming it."

**Why I Built This:**
I saw what frontier AI could do. I decided I was not going to be one of the people left behind by it. I built the stack myself. I documented it. I shipped a working product on top of it. I'm now offering to help build the bridge to communities like mine.

**Why I'm Writing This Now:**
Not because it looks good on paper. I'm writing because I've already built the thing. Now I'm looking for the support structure that matches what I'm actually doing.

---

## PART EIGHT: THE NORTH STAR — THE COMPLETE VISION

### What This Becomes When It Works

A distributed network of AI-powered service operators, each running their own machine, each connected through MCP, each serving real clients in their own communities, each building toward ownership of the physical infrastructure where they live and work.

The MCP runs the whole system:
- Client intake
- Task dispatch
- Quality checks
- Payment processing
- Audit logs
- Operator communication

Every service feeds every other service:
- Reentry Desk clients become Job Application clients
- Job Application clients become Event Childcare subscribers
- Event Childcare families become Arcade members
- Arcade members rent office space or boarding rooms in the Building
- Everyone benefits from the off-grid power system and zero-overhead operations

**The end result:** A self-reinforcing ecosystem where:
- No one operator is enslaved to operations
- Everyone sees immediate return on their contribution
- The business grows from success, not from extraction
- AI capabilities reach the people who need them most
- Economic freedom becomes possible for people it's been locked away from

### The Alternative

Without partnership: I build this alone. It takes longer. It scales slower. It reaches fewer people. The MCP is open-source; the network becomes fragmented. Communities miss the opportunity to deploy it quickly.

With partnership: Anthropic becomes the company that made this possible. Claude becomes the AI that opened doors in communities it was never supposed to reach. The Partner Network becomes the infrastructure for this kind of distributed work.

**That's the opportunity.**

---

## NEXT STEPS

I am asking for one of two outcomes:

**Option 1:** A 30-minute conversation with whoever owns partnerships or strategic dev at Anthropic  
**Option 2:** A reply pointing me to who at Anthropic should read this

I can prepare:
- A demo of Master AI running on my machine
- A walkthrough of the MCP architecture (reentry-desk server, Pupil integration, executor framework, audit log)
- Specifics about the communities I reach and their needs
- A video recording of the build journey
- The complete working code (currently private, happy to grant read access)

---

## CONTACT

**Elijah W. Sr.**  
Indianapolis, IN  
ebey317@gmail.com  
github.com/ebey317

**Submit this proposal to:**
- partnerships@anthropic.com (Claude Partner Network membership)
- business-development@anthropic.com (Strategic partnership discussion)

**Or visit:**
- claude.com/partners (Partner Network application)

---

## CLOSING

I'm not writing this letter to thank Anthropic.

I'm writing because I have something real to bring back to you: a finished product, a credential built out of life and not credentials, and access to people you can't reach without somebody like me.

The work you're doing on Claude matters to people whose names aren't in your customer database yet. Some of those people are in my contact list. I built the bridge to them already.

I'm asking whether you want to walk across it with me.

---

**If you want the rest of the world to see what I see, call me.**

Sincerely,

**Elijah W. Sr.**  
Indianapolis, Indiana  
ebey317@gmail.com

---

## APPENDIX A: BUSINESS PORTFOLIO AT A GLANCE

| Service | Price | Year 1 Revenue | Scalability | Timeline |
|---------|-------|----------------|-------------|----------|
| Reentry Desk | $400-500/mo contracts | $40K-80K | B2B orgs + grants | Weeks |
| Job Applications | $2 retail / $300-500 B2B | $12K-18K/mo | Multiple contracts | Weeks |
| Resume Builds | $75-150 per | $18K-36K/yr | Self-paced | Ongoing |
| Event Childcare | $50-75/child or subscription | $75K-100K | 20+ anchor events | 3-6 mo |
| The Arcade | $30-50 membership | $500K-1.5M | Single location proves model | 3-5 years |
| The Building | Ownership asset | $180K-250K combined | Revenue generations | 5-8 years |

**Phase 1-2 Reality:** $3,500-4,000/month from Reentry Desk, Job Apps, Resume Builds, and early Event Childcare = full-time income without the HVAC job

**Phase 3-5 Vision:** Multi-operator network, arcade operations, physical asset ownership, $1M+ net worth

---

## APPENDIX B: THE DISTRIBUTED NETWORK PHASES AT A GLANCE

**Phase 1 (Now):** You prove it solo  
**Phase 2 (Month 6-12):** Trusted people join and help  
**Phase 3 (Month 12-24):** Multiple operators + revenue split model  
**Phase 4 (Month 24-36):** LLC formation + Claude Team workspace  
**Phase 5 (Year 3+):** Central workstation + The Building  

Each phase triggers when the previous phase proves sustainable.

---

## APPENDIX C: WHY THIS MATTERS AT THE ANTHROPIC LEVEL

Anthropic's mission is to build AI that is helpful, harmless, and honest.

There's a built-in tension: if frontier AI only reaches people who already have resources, it concentrates power in the demographics that least need it.

To genuinely fulfill that mission, Anthropic has to reach beyond the current user base. That reach is hard to manufacture credibly from inside a well-funded SF lab. It's much more credible when it comes from someone who lives the constraints.

I am that someone. There will be others. Building infrastructure for partnerships like this is something Anthropic will likely have to do anyway in the next 12-24 months.

I would like to be among the first.

---

**End of Master Proposal**
