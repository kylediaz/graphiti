import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from neo4j import AsyncDriver

from graphiti_core.edges import CommunityEdge
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import CommunityNode, EntityNode, get_community_node_from_record
from graphiti_core.prompts import prompt_library
from graphiti_core.utils.maintenance.edge_operations import build_community_edges

logger = logging.getLogger(__name__)


async def build_community_projection(driver: AsyncDriver) -> str:
    records, _, _ = await driver.execute_query("""
    CALL gds.graph.project("communities", "Entity",             
        {RELATES_TO: {
            type: "RELATES_TO",
            orientation: "UNDIRECTED",
            properties: {weight: {property: "*", aggregation: "COUNT"}}
        }}
    )
    YIELD graphName AS graph, nodeProjection AS nodes, relationshipProjection AS edges
    """)

    return records[0]['graph']


async def destroy_projection(driver: AsyncDriver, projection_name: str):
    await driver.execute_query(
        """
    CALL gds.graph.drop($projection_name)
    """,
        projection_name=projection_name,
    )


async def get_community_clusters(
    driver: AsyncDriver, projection_name: str
) -> list[list[EntityNode]]:
    records, _, _ = await driver.execute_query("""
    CALL gds.leiden.stream("communities")
    YIELD nodeId, communityId
    RETURN gds.util.asNode(nodeId).uuid AS entity_uuid, communityId
    """)
    community_map: dict[int, list[str]] = defaultdict(list)
    for record in records:
        community_map[record['communityId']].append(record['entity_uuid'])

    community_clusters: list[list[EntityNode]] = list(
        await asyncio.gather(
            *[EntityNode.get_by_uuids(driver, cluster) for cluster in community_map.values()]
        )
    )

    return community_clusters


async def summarize_pair(llm_client: LLMClient, summary_pair: tuple[str, str]) -> str:
    # Prepare context for LLM
    context = {'node_summaries': [{'summary': summary} for summary in summary_pair]}

    llm_response = await llm_client.generate_response(
        prompt_library.summarize_nodes.summarize_pair(context)
    )

    pair_summary = llm_response.get('summary', '')

    return pair_summary


async def generate_summary_description(llm_client: LLMClient, summary: str) -> str:
    context = {'summary': summary}

    llm_response = await llm_client.generate_response(
        prompt_library.summarize_nodes.summary_description(context)
    )

    description = llm_response.get('description', '')

    return description


async def build_community(
    llm_client: LLMClient, community_cluster: list[EntityNode]
) -> tuple[CommunityNode, list[CommunityEdge]]:
    summaries = [entity.summary for entity in community_cluster]
    length = len(summaries)
    while length > 1:
        odd_one_out: str | None = None
        if length % 2 == 1:
            odd_one_out = summaries.pop()
            length -= 1
        new_summaries: list[str] = list(
            await asyncio.gather(
                *[
                    summarize_pair(llm_client, (str(left_summary), str(right_summary)))
                    for left_summary, right_summary in zip(
                        summaries[: int(length / 2)], summaries[int(length / 2) :]
                    )
                ]
            )
        )
        if odd_one_out is not None:
            new_summaries.append(odd_one_out)
        summaries = new_summaries
        length = len(summaries)

    summary = summaries[0]
    name = await generate_summary_description(llm_client, summary)
    now = datetime.now()
    community_node = CommunityNode(
        name=name,
        group_id=community_cluster[0].group_id,
        labels=['Community'],
        created_at=now,
        summary=summary,
    )
    community_edges = build_community_edges(community_cluster, community_node, now)

    logger.info((community_node, community_edges))

    return community_node, community_edges


async def build_communities(
    driver: AsyncDriver, llm_client: LLMClient
) -> tuple[list[CommunityNode], list[CommunityEdge]]:
    projection = await build_community_projection(driver)
    community_clusters = await get_community_clusters(driver, projection)

    communities: list[tuple[CommunityNode, list[CommunityEdge]]] = list(
        await asyncio.gather(
            *[build_community(llm_client, cluster) for cluster in community_clusters]
        )
    )

    community_nodes: list[CommunityNode] = []
    community_edges: list[CommunityEdge] = []
    for community in communities:
        community_nodes.append(community[0])
        community_edges.extend(community[1])

    await destroy_projection(driver, projection)
    return community_nodes, community_edges


async def remove_communities(driver: AsyncDriver):
    await driver.execute_query("""
    MATCH (c:Community)
    DETACH DELETE c
    """)


async def determine_entity_community(
    driver: AsyncDriver, entity: EntityNode
) -> tuple[CommunityNode | None, bool]:
    # Check if the node is already part of a community
    records, _, _ = await driver.execute_query(
        """
    MATCH (c:Community)-[:HAS_MEMBER]->(n:Entity {uuid: $entity_uuid})
    RETURN
        c.uuid As uuid, 
        c.name AS name,
        c.name_embedding AS name_embedding,
        c.group_id AS group_id,
        c.created_at AS created_at, 
        c.summary AS summary
    """,
        entity_uuid=entity.uuid,
    )

    if len(records) > 0:
        return get_community_node_from_record(records[0]), False

    # If the node has no community, add it to the mode community of surrounding entities
    records, _, _ = await driver.execute_query(
        """
    MATCH (c:Community)-[:HAS_MEMBER]->(m:Entity)-[:RELATES_TO]-(n:Entity {uuid: $entity_uuid})
    RETURN
        c.uuid As uuid, 
        c.name AS name,
        c.name_embedding AS name_embedding,
        c.group_id AS group_id,
        c.created_at AS created_at, 
        c.summary AS summary
    """,
        entity_uuid=entity.uuid,
    )

    communities: list[CommunityNode] = [
        get_community_node_from_record(record) for record in records
    ]

    community_map: dict[str, int] = defaultdict(int)
    for community in communities:
        community_map[community.uuid] += 1

    community_uuid = None
    max_count = 0
    for uuid, count in community_map.items():
        if count > max_count:
            community_uuid = uuid
            max_count = count

    if max_count == 0:
        return None, False

    for community in communities:
        if community.uuid == community_uuid:
            return community, True

    return None, False


async def update_community(
    driver: AsyncDriver, llm_client: LLMClient, embedder, entity: EntityNode
):
    community, is_new = await determine_entity_community(driver, entity)

    if community is None:
        return

    new_summary = await summarize_pair(llm_client, (entity.summary, community.summary))
    new_name = await generate_summary_description(llm_client, new_summary)

    community.summary = new_summary
    community.name = new_name

    if is_new:
        community_edge = (build_community_edges([entity], community, datetime.now()))[0]
        await community_edge.save(driver)

    await community.generate_name_embedding(embedder)

    await community.save(driver)
