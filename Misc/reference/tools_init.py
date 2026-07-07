"""
GROUND TRUTH: upstream Batfish MCP tool package __init__.py
Source: Presidio-Federal/batfish-mcp-container : batfish/tools/__init__.py
Use this to confirm exact tool identifiers. The running container's tools/list
is the final authority (middleware may rename/hide).

Tool families present:
  Core what-if : run_tagged_tests, get_inventory, check_routing (ProtocolType),
                 simulate_traffic, failure_impact (FailureType)
  AWS (hide)   : aws_reachability, aws_trace_route, aws_internet_exposure,
                 aws_security_evaluation, aws_subnet_segmentation,
                 aws_route_analysis, aws_node_inventory, aws_change_impact,
                 aws_find_unrestricted_ssh
  Network      : network_generate_topology, network_list_subnets,
                 network_list_boundary_devices, network_reachability_summary,
                 network_summary, network_segment, network_vlan_discovery,
                 network_get_allowed_services, network_classify_devices,
                 network_analyze_acl_rules, network_traceroute,
                 network_bidirectional_reachability, network_vlan_device_count,
                 network_device_connections, network_interface_vlan_count,
                 network_node_inventory, network_topology_connections
  Management   : list_networks, list_snapshots, delete_network, delete_snapshot,
                 get_snapshot_info, get_parse_status, cleanup
  Compliance   : update_classification_rules, list_classification_rules,
                 check_zone_compliance, auto_classify_zones,
                 get_enforcement_points, list_models   (hide for what-if)
  Init - AWS   : init_aws_snapshot, aws_add_data_chunk, aws_finalize_snapshot,
                 aws_remove_chunk, aws_view_staging
  Init - Net   : network_prepare_snapshot, network_finalize_snapshot,
                 network_view_staging, network_remove_config,
                 network_upload_zip, init_snapshot
  Init - GitHub: github_snapshot   (hide; we upload directly)

See docs/03_tool_inventory.md for the USE / HIDE split and the two schema
unknowns to confirm from source.
"""
