NetGuard-CIA demo scenarios
===========================

Each folder is a self-contained demo taken from the official Batfish examples
(github.com/batfish/pybatfish, jupyter_notebooks/networks + the matching
notebooks). Every folder contains:

  configs/            the device configs to upload in the app sidebar
  QUESTIONS.txt       what the network is, the questions to ask in the chat,
                      and what the notebook says you should expect
  network-diagram.png topology picture (where the upstream repo provides one)

How to demo
-----------
1. Start the stack + app (see the repo README quickstart), open
   http://localhost:8501
2. Pick a scenario folder, upload every .cfg from its configs/ directory,
   click "Build snapshot"
3. Ask the questions from QUESTIONS.txt one at a time
4. "Reset session" in the sidebar before switching to another scenario

The scenarios
-------------
1 - failure impact and chaos monkey  13 city routers; node/link failures and
                                     stacked "now also fail X" turns
2 - link failure with failover       the classic AS1/AS2/AS3 lab; failover
                                     verdicts, stacking, seeded config defects
3 - acl and firewall rules           2 devices; filter permit/deny questions
                                     with crisp expected answers
4 - bgp session debugging            AS lab with purposely broken BGP
                                     sessions of every flavor
5 - route analysis                   2 routers; small enough to verify every
                                     engine fact by hand

All five config sets are verified to parse cleanly in this app's Batfish
stack (all Cisco IOS).
