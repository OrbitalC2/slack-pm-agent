"""
Memory service — embedding and semantic retrieval for project context.

Uses OpenAI text-embedding-3-small (1536 dims) fixed for MVP.
"""

from uuid import UUID

from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.memory_queries import insert_memory_chunk, search_similar_chunks
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.logging_config import get_logger

logger = get_logger("memory_service")


async def embed_and_store(
    project_id: UUID,
    text: str,
    source_type: str,
    source_id: UUID | None = None,
    importance: int = 3,
) -> None:
    """
    Compute embedding for text and store as a memory chunk.

    Args:
        project_id: The project this memory belongs to
        text: The text content to embed and store
        source_type: gather_summary, huddle_summary, decision, task, clarification, etc.
        source_id: Optional reference to the source record
        importance: 1-5 importance score
    """
    try:
        llm_client = get_llm_client()
        embedding = await llm_client.embed(text)

        session_factory = get_session_factory()
        async with session_factory() as db:
            await insert_memory_chunk(
                db,
                project_id=project_id,
                source_type=source_type,
                text_content=text,
                embedding=embedding,
                source_id=source_id,
                importance=importance,
            )
            await db.commit()

        logger.debug(f"Stored memory chunk: type={source_type}, project={project_id}")

    except Exception as e:
        logger.error(f"Failed to embed and store memory: {e}", exc_info=True)
        # Still try to store without embedding
        try:
            session_factory = get_session_factory()
            async with session_factory() as db:
                await insert_memory_chunk(
                    db,
                    project_id=project_id,
                    source_type=source_type,
                    text_content=text,
                    embedding=None,
                    source_id=source_id,
                    importance=importance,
                )
                await db.commit()
            logger.info("Stored memory chunk without embedding (fallback)")
        except Exception as inner_e:
            logger.error(f"Failed to store memory even without embedding: {inner_e}")


async def retrieve_relevant(
    project_id: UUID,
    query: str,
    limit: int = 5,
) -> list[dict]:
    """
    Retrieve semantically relevant memory chunks for a query.

    Returns a list of dicts with text, source_type, source_id, importance.
    """
    try:
        llm_client = get_llm_client()
        query_embedding = await llm_client.embed(query)

        session_factory = get_session_factory()
        async with session_factory() as db:
            chunks = await search_similar_chunks(
                db,
                project_id=project_id,
                query_embedding=query_embedding,
                limit=limit,
            )

            return [
                {
                    "text": chunk.text,
                    "source_type": chunk.source_type,
                    "source_id": str(chunk.source_id) if chunk.source_id else None,
                    "importance": chunk.importance,
                }
                for chunk in chunks
            ]

    except Exception as e:
        logger.error(f"Failed to retrieve relevant memory: {e}", exc_info=True)
        return []
