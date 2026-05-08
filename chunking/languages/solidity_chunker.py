"""Solidity-specific tree-sitter based chunker."""

from typing import Any, Dict, Set

from chunking.base_chunker import LanguageChunker


class SoliditySolChunker(LanguageChunker):
    """Solidity-specific chunker using tree-sitter.

    Top-level chunkable units are contract / library / interface / abstract
    contract declarations. Inside those bodies we split on functions,
    modifiers, constructors, fallback/receive, structs, enums, events, and
    custom errors so each becomes its own searchable chunk.
    """

    # Top-level type containers — these become parents for nested chunks.
    _CONTAINER_NODES = {
        'contract_declaration',
        'library_declaration',
        'interface_declaration',
    }

    # Nested members inside a contract/library/interface body.
    _MEMBER_NODES = {
        'function_definition',
        'modifier_definition',
        'constructor_definition',
        'fallback_receive_definition',
        'struct_declaration',
        'enum_declaration',
        'event_definition',
        'error_declaration',
    }

    def __init__(self):
        super().__init__('solidity')

    def _get_splittable_node_types(self) -> Set[str]:
        return self._CONTAINER_NODES | self._MEMBER_NODES

    def _get_recursable_container_types(self) -> Set[str]:
        # Recurse into contract/library/interface bodies so functions,
        # modifiers, structs, enums, events, errors are chunked individually.
        return self._CONTAINER_NODES

    def extract_metadata(self, node: Any, source: bytes) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {'node_type': node.type}

        # Name: first identifier child for most node types. fallback_receive
        # nodes have no name (they're implicit `fallback`/`receive`); use the
        # leading keyword text as the name for clarity.
        name = None
        for child in node.children:
            if child.type == 'identifier':
                name = self.get_node_text(child, source)
                break
        if not name and node.type == 'fallback_receive_definition':
            # Grab the first leaf token (`fallback` or `receive`).
            head = node.children[0] if node.children else None
            if head is not None:
                name = self.get_node_text(head, source)
        if not name and node.type == 'constructor_definition':
            name = 'constructor'
        if name:
            metadata['name'] = name

        # `abstract contract Foo` is still a contract_declaration; surface the
        # `abstract` modifier as a flag so the chunk metadata reflects it.
        if node.type == 'contract_declaration':
            text_before_name = source[node.start_byte:node.start_byte + 64]
            if b'abstract' in text_before_name.split(b'contract', 1)[0]:
                metadata['is_abstract'] = True

        # Visibility / state mutability are sibling children of functions and
        # state variables; expose as semantic tags.
        for child in node.children:
            if child.type == 'visibility':
                metadata['visibility'] = self.get_node_text(child, source)
            elif child.type == 'state_mutability':
                metadata['state_mutability'] = self.get_node_text(child, source)
            elif child.type == 'virtual':
                metadata['is_virtual'] = True
            elif child.type == 'override_specifier':
                metadata['is_override'] = True

        return metadata
