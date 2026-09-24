# Fabric Topology

The **Topology** page in `fcli server` draws an interactive fabric diagram derived directly from LLDP and device state, rendered one tier per row. Rather than relying on static inventory labels, it infers what each node is dynamically from what services and configurations it runs.

## Tier inference

Nodes are classified into tiers based on their operational roles:

* **Leaf**: A node with configured **mac-vrfs** (and optionally ip-vrfs). This is the access tier where services meet host ports.
* **DCGW (Data Center Gateway)**: A node whose services carry **two enabled `bgp-vpn` instances**. The second instance represents the WAN side of a stitched service, characteristic of a gateway out of the data center.
* **Spine**: A node with **no mac-vrf and no ip-vrf** that connects to two or more leaves. It interconnects the leaves without terminating customer services.
* **WAN / Core**: Anything else without services — such as P/PE routers and super-spines that transit traffic across the fabric but do not attach directly to leaves.

## Client tier inference

Below the network nodes sits the **client** tier. Clients are not inventory nodes; they represent endpoints that services face:
* A bridged sub-interface of a mac-vrf
* A routed port of an ip-vrf

Only ports facing external endpoints count. Internal interfaces — such as the WAN sub-interface of a stitched ip-vrf (which connects to a DCGW neighbor), IRBs, loopbacks, and `system0` — are excluded.

### Client grouping

Multiple VLANs on one physical cable are collapsed into a single client. Determining which ports belong to the same client is resolved using the best available information:
1. **LLDP neighbour name**: An unmatched LLDP neighbour advertising its identity presents the same name across all connected leaves.
2. **ESI (Ethernet Segment Identifier)**: If an external client runs no LLDP, the ESI of its multi-homed ethernet segment groups its ports across leaves into a single box spanning those leaves.
3. **Port identifier**: Failing LLDP or ESI, the port itself serves as the identifier.

Every client box is simply labeled `client`. What it was named after is a heuristic (drawn from an ESI or port), so avoiding an artificial hostname prevents presenting a false identity. Clicking a client box lists all sub-interfaces attaching to it (service, VLAN, IP address) and provides one-click navigation to the connected leaf.

## Ethernet segments

Between the leaves and multi-homed clients sits the **ethernet-segments** tier. A multi-homed client connects to multiple leaves over a bundle (such as an LACP LAG) rather than individual independent links. Because this bundle is a distinct configured object with its own ESI—and often where multi-homing misconfigurations arise—it is drawn as its own tier.

A segment box displays `ES` followed by the last two bytes of its ESI (the discriminating identifier in a fabric). The detail panel shows the local name each leaf assigned to the segment, highlighting any naming disagreements across leaves.

## Cabling and link visualization

* **De-duplication**: Cables are discovered via LLDP. Because both endpoints report the link, reciprocal reports are de-duplicated.
* **Parallel links**: Multiple parallel links between two nodes are collapsed into a single line annotated with `2×`, `3×`, etc.
* **Status coloring**: Links are color-coded based on the operational state of the connected interface ports.
* **Interactivity**: Hovering over a node dims all unrelated nodes and links; clicking a node displays its configured services and per-port peer list, allowing hop-by-hop traversal of the fabric.

## Multi-fabric graph partitioning

A single inventory may contain multiple disjoint fabrics. Nodes that share no inter-switch cabling are treated as separate topologies, even if servers are dual-homed across them:
* Each connected component of the switch cabling graph receives its own **tab**, ordered by size (largest first).
* An **All** tab displays all components side by side.
* A client dual-homed to two separate fabrics is displayed on both tabs; clicking a link to the other fabric automatically switches tabs.
* **Tab naming**: Tabs are named based on shared site attributes or common node naming prefixes (e.g., `frontend-leaf1` and `frontend-spine1` produce a `frontend` tab). If a coherent naming pattern cannot be found for all fabrics, standard ordinal ranking is used to maintain consistency.
* **Unattached nodes**: Nodes with no cables (or whose LLDP data has not yet arrived) are placed on an **Unattached** tab rather than creating individual single-node tabs.

## Navigation and controls

* **Zoom and pan**: Navigate large fabrics using `−` / `+` buttons, `ctrl` + mouse wheel (or trackpad pinch), or `-`, `+`, and `0` keyboard shortcuts. Dragging pans the canvas without accidentally triggering node selection.
* **Fit to window**: The **fit** button scales the active fabric to fit the browser viewport and dynamically adjusts as detail panels open or close. Manual zoom levels are persisted across page reloads.
* **Outside & unclassified nodes**: Neighbours discovered via LLDP that do not match inventory nodes are drawn as *outside* nodes. Inventory nodes that have not yet streamed telemetry are rendered as *unclassified* rather than omitted.

## Health overlay and lost cables

The graph `/api/topology` returns is annotated with what the checks found (see [Health](health.md)):

* every node carries `findings` (counts by severity), `health` (the worst of them) and `issues` (the findings themselves), drawn as a badge on its corner;
* every link carries the `findings` on either of its ends and a `health`, which the **health** overlay colours it by;
* `incidents` lists the incidents, and `summary` briefs the fabric in a few lines above the drawing.

The **service** overlay lights up the nodes and clients that carry one mac-vrf or ip-vrf, from the service names each node reports (`services`).

A cable LLDP no longer reports is not dropped. The server remembers every adjacency it has seen - kept on disk per fabric in `~/.local/state/fcli/cabling/`, so a server restarted during an outage still knows it - and draws a cable both ends have lost from memory, marked `lost` and dotted, in the state its ports are in now. A link that went down is the one that matters, so it stays on the drawing.


## Virtual ethernet-segments (L3 aliasing)

A virtual ethernet-segment has no port: it tracks a next-hop in a routed service, such as a host multi-homed to two leaves that advertises its own prefixes over BGP. It lets every remote VTEP load-balance those prefixes over all the leaves that can reach the host, not only the one that advertised them. None of it is a cable, so it is drawn **only in the service overlay of the ip-vrf it serves**:

* a **vES** node in the segment tier, labelled with the next-hop it tracks;
* its links to the leaves it is **attached** on, meaning the DF candidates of its election. The designated forwarder is drawn solid and tagged `DF`. Leaves that only have it configured are listed in the detail panel, not drawn;
* a dotted link to the **client that owns the next-hop**: the client attached, on an attached leaf, to the bridge domain whose IRB subnet holds the next-hop;
* an `L3 alias` link from every **remote VTEP that actually load-balances over it**. The evidence is that VTEP's own route table in the ip-vrf: the next-hop's host route installed over two or more of the attached VTEPs. The detail panel lists the prefixes that resolve through it.

Each node elects the DF itself. When they disagree, which on a single-active segment means two leaves forwarding, the detail panel shows every node's view, and the `es_df` check raises it as an incident: *"ethernet-segment vES-host6-tenant1: designated forwarder disagreement in ipvrf-1"*.
