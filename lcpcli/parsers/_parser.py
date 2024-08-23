"""
Here we create an abstract base class from which all parsers should be derived.

Parsers require a .parse() and a .write() method at the least.

Note that .write() doesn't write to file, but returns a string that can be written to file!

TODO: create a class for the entries in self._tables
TODO: it needs to be easy to make lookup files like *_form for that class too
TODO: update meta.json when new attributes are discovered
"""

import abc
import json
import os
import re

from ..utils import (
    is_time_anchored,
    get_ci,
    Info,
    Table,
    TokenTable,
    LookupTable,
    EntityType,
    Document,
    Categorical,
    Dependency,
    Meta,
    Text,
    Sentence,
    NestedSet,
)


class Parser(abc.ABC):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.char_range_cur = 1
        self.frame_range_cur = 0
        self._tables = {}
        self.doc_frames = {}
        self.config = kwargs.get("config", {})

    @abc.abstractmethod
    def parse(self, content):
        """
        Turn a string of content into our abstract JSON format
        Format: {doc_id:{'meta':{},'sentences':{sent_id:{'meta':{},'text':{'id','form',...}}, ...}}}
        """
        pass

    @abc.abstractmethod
    def parse_generator(self, generator, config={}):
        """
        Takes an object with a reader method and yields (sentence,doc)
        """
        pass

    @abc.abstractmethod
    def write(self, content, filename=None, combine=True, meta={}):
        """
        Create a writeable string from JSON data
        Content should use our abstract JSON format
        """
        pass

    @abc.abstractmethod
    def write_generator(self, generator):
        """
        Takes a generator of (sentence, doc)'s and yields a sentence string
        """
        pass

    @abc.abstractmethod
    def combine(self, content):
        """
        Combine a dictionary of {original_filepath: json_representation}
        """
        pass

    def compute_doc(self, content, first_sentence, last_sentence):
        """
        Return (doc_id,char_range,meta) for a given pair of first and last sentences
        """
        meta_obj = {}
        start_idx = re.search(self.start_idx, first_sentence)[0]
        end_idx = re.search(self.end_idx, last_sentence)[0]
        char_range = f"{start_idx},{end_idx}"

        # meta_lines = [line for line in content.split("\n\n") if line.startswith("# text")]
        meta_lines = [
            line for line in content.split("\n") if line.startswith("# newdoc ")
        ]
        for line in meta_lines:
            if " = " not in line:
                continue
            k, v = line.split(" = ")
            meta_obj[k[9:].strip()] = v.strip()

        # if "text_id" in meta_obj:
        if "id" in meta_obj:
            doc_id = meta_obj.pop("id")
        else:
            self.text_id += 1
            doc_id = self.text_id

        return doc_id, char_range, json.dumps(meta_obj)

    def upload_new_doc(self, doc, table, doc_name="document"):
        """
        Take a document instance, a character_range cursor and a file handle, and write a line to the file
        """
        meta = doc.attributes.get("meta", {})
        col_names = [f"{doc_name}_id", "char_range"]
        cols = [
            str(table.cursor),
            f"[{str(doc.char_range_start)},{str(self.char_range_cur-1)})",
        ]
        if doc.frame_range[1] != 0:
            col_names.append("frame_range")
            cols.append(f"[{str(doc.frame_range[0])},{str(doc.frame_range[1])})")
            doc_frame_id = str(meta._value.get("name", doc.id) if meta else doc.id)
            self.doc_frames[doc_frame_id] = [*doc.frame_range]
        name_doc = str(table.cursor)
        if meta:
            if "name" in meta._value:
                # name_doc = meta._value.pop("name")
                name_doc = meta._value.get("name")
            if meta._value:
                col_names.append("meta")
                cols.append(str(meta.value).strip())
        for attr_name, attr in doc.attributes.items():
            if attr_name == "meta":
                continue
            col_names.append(attr_name.lower())
            cols.append(str(attr.value).strip())
        media_slots = self.config.get("meta", {}).get("mediaSlots", {})
        if media_slots:
            media = doc.attributes.get("media", Meta("dummy", {})).value
            for name, attribs in media_slots.items():
                assert (
                    attribs.get("isOptional") is not False or name in media
                ), KeyError(
                    f"Filename missing for required media '{name}' from document {doc.id}"
                )
            if any(
                x.get("mediaType") in ("audio", "video") for x in media_slots.values()
            ):
                col_names.append("name")
                cols.append(name_doc)
        if table.cursor == 1:
            table.write(col_names)
        table.write(cols)
        table.cursor += 1

    def write_token_deps(self, table, working_on=""):
        """
        Take a file handle with a 'deps' key that contains NestedSet's to be linked and write to file, then clear memory
        """
        # head_id is None: this is a new head, process the previous one
        empty_segments = []
        for segment_id, tokens in table.deps.items():
            if segment_id == working_on:
                continue
            nested_set_of_previous_head = None
            tokens = {k: v for k, v in tokens.items() if not isinstance(k, Info)}
            for token_id, attrs in tokens.items():
                # Link the nested sets from the dependencies in memory
                hid = attrs["head_id"]
                if hid == "":
                    nested_set_of_previous_head = attrs["nested_set"]
                if hid not in tokens:
                    continue
                tokens[hid]["nested_set"].add(tokens[token_id]["nested_set"])
            anchor_right = table.anchor_right
            try:
                assert nested_set_of_previous_head, AssertionError(
                    f"Moved to a new segment without finding the head of the dependencies in the previous segment"
                )
            except:
                continue
            if nested_set_of_previous_head.consumed:
                continue
            nested_set_of_previous_head.compute_anchors()
            for id in nested_set_of_previous_head.all_ids:
                nested_set = tokens[id]["nested_set"]
                if nested_set.consumed:
                    continue
                parent_id = (
                    ""
                    if nested_set.parent is None
                    else str(nested_set.parent.cursor_id)
                )
                table.write(
                    [
                        str(parent_id),  # head
                        str(str(nested_set.cursor_id)),  # dependent (self)
                        nested_set.label,  # label
                        str(anchor_right + nested_set.left),  # left_anchor
                        str(anchor_right + nested_set.right),  # right_anchor
                    ]
                )
                nested_set.consumed = True
            table.anchor_right = anchor_right + nested_set_of_previous_head.right
            nested_set_of_previous_head.consumed = True
            # Now clear the processed tokens
            for id in nested_set_of_previous_head.all_ids:
                tokens.pop(id)
            if not tokens:
                empty_segments.append(segment_id)
        # Clear the segments with no tokens left
        for s_id in empty_segments:
            table.deps.pop(s_id)

    def aligned_entity(self, entity, path, attribute, aligned_entities={}):
        aname_low = attribute.name.lower()
        assert isinstance(attribute, Text), TypeError(
            f"Foreign key '{attribute.name}' should be a simple text"
        )
        layer_name = next(
            (x for x in self.config.get("layer", {}).keys() if x.lower() == aname_low),
            None,
        )
        layer_attributes = (
            self.config["layer"].get(layer_name, {}).get("attributes", {})
            if layer_name
            else {}
        )
        contained_entity = aligned_entities[aname_low]["properties"].get("contains", "")
        has_frame_range = is_time_anchored(
            self.config["layer"].get(contained_entity, {}), self.config
        )
        # Create a table for the entity if it doesn't exist yet
        if aname_low not in self._tables:
            self._tables[aname_low] = Table(
                aname_low, path, config=get_ci(self.config["layer"], layer_name)
            )
            table = self._tables[aname_low]
            with open(aligned_entities[aname_low]["fn"], "r") as aligned_file:
                table.col_names = aligned_file.readline().rstrip().split("\t")[1:]
                entity_col_names = [f"{aname_low}_id"]
                for cn in table.col_names:
                    attribute_props = layer_attributes.get(cn, None)
                    assert attribute_props is not None, ReferenceError(
                        f"Attribute {cn} not found for entity {attribute.name}"
                    )
                    if attribute_props.get("type", "") == "text":
                        entity_col_names.append(cn + "_id")
                        lookup_table = LookupTable(aname_low, cn, path, self.config)
                        self._tables[f"{aname_low}_{cn}"] = lookup_table
                    else:
                        entity_col_names.append(cn)
                entity_col_names.append("char_range")
                if has_frame_range:
                    entity_col_names.append("frame_range")
                table.write(entity_col_names)
        table = self._tables[aname_low]
        fk = attribute.value.strip()
        ce = table.current_entity
        # Still processing the same entity: delay until we hit something else
        if fk == ce.get("id", ""):
            table.previous_entity = entity
        else:
            # No longer processing the previous entity
            if ce:
                entity_cols = ce["cols"]
                # Process labels
                lbls = table.labels
                for n, col_name in enumerate(table.col_names):
                    ctype = layer_attributes.get(col_name, {}).get("type", "")
                    if ctype != "labels":
                        continue
                    bits = int("0", 2)
                    for label in entity_cols[n].split(","):
                        l = label.strip()
                        idx = lbls.get(l, len(lbls))
                        lbls[l] = idx
                        bs = "1" + "".join(["0" for _ in range(idx - 1)])
                        bits = bits | int(bs, 2)
                    entity_cols[n] = bin(bits)[2:]
                range_up = self.char_range_cur - 1  # Stop just before this entity
                if range_up <= int(ce["range_low"]):
                    range_up = int(ce["range_low"]) + 1
                cols_to_write = [
                    table.cursor,
                    *entity_cols,
                    f"[{str(ce['range_low'])},{str(range_up)})",
                ]
                if has_frame_range:
                    lower_frame_range, upper_frame_range = (
                        int(ce["frame_range_start"]),
                        self.frame_range_cur,
                    )
                    if upper_frame_range <= lower_frame_range:
                        upper_frame_range = lower_frame_range + 1
                    cols_to_write.append(
                        f"[{str(lower_frame_range)},{str(upper_frame_range)})"
                    )
                table.write(cols_to_write)
                table.cursor += 1
            # Create an empty entity dict if no ID was provided
            if not fk or fk.strip() == "_":
                ce = {}
            else:
                ce = {"id": fk}
                # Read the content of the entity from the provided file
                with open(aligned_entities[aname_low]["fn"], "r") as aligned_file:
                    while True:
                        line = aligned_file.readline()
                        if not line:
                            break
                        line = line.split("\t")
                        if line[0].strip() == fk:
                            ce["cols"] = []
                            for n, col in enumerate(line[1:]):
                                col_name = table.col_names[n]
                                attr_name = next(
                                    (
                                        x
                                        for x in layer_attributes
                                        if x.lower() == col_name
                                        or x.lower() + "_id" == col_name
                                    ),
                                    col_name,
                                )
                                ctype = layer_attributes.get(attr_name, {}).get("type")
                                if ctype == "text":
                                    lookup_table = self._tables[
                                        f"{aname_low}_{col_name}"
                                    ]
                                    ce["cols"].append(lookup_table.get_id(col.strip()))
                                else:
                                    ce["cols"].append(col.strip())
                                if ctype == "categorical":
                                    if attr_name not in table.categorical_values:
                                        table.categorical_values[attr_name] = set()
                                    table.categorical_values[attr_name].add(col.strip())
                            ce["range_low"] = str(self.char_range_cur)
                            break
                if has_frame_range:
                    ce["frame_range_start"] = str(self.frame_range_cur)
            table.current_entity = ce

    def close_aligned_entity(self, name, path, aligned_entities={}):
        dummy_entity = EntityType()
        dummy_attribute = Text(name, "dummy")
        self.aligned_entity(dummy_entity, path, dummy_attribute, aligned_entities)
        if name in self._tables:
            self._tables[name].current_entity = {}

    def close_upload_files(self, path="./"):
        if self._tables is None:
            return
        # Close the files
        for n, tab in self._tables.items():
            tab.file.close()
            if not tab.labels:
                continue
            # Write the labels
            nlabels = len(tab.labels)
            with open(os.path.join(path, f"{n}_labels.csv"), "w") as f:
                f.write("\t".join(["bit", "label"]) + "\n")
                for l, i in tab.labels.items():
                    f.write("\t".join([str(i), str(l)]) + "\n")
            tab.config["nlabels"] = nlabels
            # Pad 0s to match the bit length
            label_attributes = {}
            for n, v in tab.config.get("attributes", {}).items():
                # List all the attributes of type "labels"
                if v.get("type") != "labels":
                    continue
                label_attributes[n] = -1
            if not label_attributes:
                continue
            with open(tab.path, "r") as input, open(tab.path + ".tmp", "w") as output:
                while True:
                    il = input.readline()
                    if not il:
                        break
                    il = il.rstrip()
                    cols = il.split("\t")
                    if next(x for x in label_attributes.values()) == -1:
                        # First line = header
                        for n, c in enumerate(cols):
                            for la in label_attributes:
                                if not c.lower().startswith(la.lower()):
                                    continue
                                # Report the index of the column corresponding to the attribute
                                label_attributes[la] = n
                    else:
                        # Not first line = row
                        for n in label_attributes.values():
                            bits = cols[n]
                            if len(bits) < nlabels:
                                bits = (
                                    "".join(["0" for _ in range(nlabels - len(bits))])
                                    + bits
                                )
                            cols[n] = bits
                    output.write("\t".join(cols) + "\n")
            os.rename(tab.path + ".tmp", tab.path)

    def generate_upload_files_generator(
        self,
        reader,
        path="./",
        default_doc={},
        config={},
        aligned_entities={},
        aligned_entities_segment={},
    ):
        """
        Take a reader object and outputs verticalized LCP self._tables
        """
        doc_name = "document"
        seg_name = "segment"
        tok_name = "token"
        if "firstClass" in config:
            doc_name = config["firstClass"].get("document", doc_name).lower()
            seg_name = config["firstClass"].get("segment", seg_name).lower()
            tok_name = config["firstClass"].get("token", tok_name).lower()

        self._tables = self._tables or {
            "document": Table(
                doc_name, path, config=get_ci(self.config["layer"], doc_name)
            ),
            "segment": Table(
                seg_name, path, config=get_ci(self.config["layer"], doc_name)
            ),
            "token": TokenTable(
                tok_name, path, config=get_ci(self.config["layer"], doc_name)
            ),
        }
        token_table = self._tables["token"]
        char_range_start = self.char_range_cur
        has_frame_range = False
        offset_frame_range = self.frame_range_cur
        token_have_dependencies = False
        current_document = None
        for segment, doc in self.parse_generator(reader, config=config):

            char_range_segment_start = self.char_range_cur
            frame_range_segment_start = None

            if doc:
                if current_document is not doc:
                    if current_document:
                        self.upload_new_doc(
                            current_document,
                            self._tables["document"],
                            doc_name=doc_name,
                        )
                    current_document = doc
                    current_document.frame_range[0] = offset_frame_range
                    current_document.char_range_start = char_range_segment_start

            if not segment:
                continue

            # List only the token attributes that are not null on every row
            non_null_attributes = token_table.non_null_attributes
            if not non_null_attributes and token_table.cursor == 1:
                col_names = {f"{tok_name}_id": None}
                for token in segment.tokens:
                    if token.frame_range:
                        has_frame_range = True
                    for attr_name, attr_value in token.attributes.items():
                        if not attr_value.value:
                            continue
                        non_null_attributes[attr_name] = True
                        # Dependencies and references to aligned entities will be processed separately; do not list
                        if (
                            isinstance(attr_value, Dependency)
                            or attr_name.lower() in aligned_entities
                        ):
                            continue
                        # Attributes of type Text and Meta use foreign keys
                        if any(isinstance(attr_value, klass) for klass in (Text, Meta)):
                            col_names[attr_name + "_id"] = None
                        else:
                            col_names[attr_name] = None
                token_table.non_null_attributes = non_null_attributes
                col_names["char_range"] = None
                if has_frame_range:
                    col_names["frame_range"] = None
                col_names[f"{seg_name}_id"] = None
                token_table.write([c for c in col_names])

            print(
                "segment",
                segment.id,
                "meta",
                segment.attributes.get("meta", Meta("dummy", "dummy")).value,
            )
            for token in segment.tokens:
                cols = [str(token_table.cursor)]
                for attr_name in non_null_attributes:
                    attribute = token.attributes.get(attr_name, None)
                    aname_low = attr_name.lower()

                    if attribute is None:
                        cols.append("")
                        continue

                    # For example, named_entity
                    if aname_low in aligned_entities and isinstance(attribute, Text):
                        self.aligned_entity(token, path, attribute, aligned_entities)

                    # For example, xpos
                    elif isinstance(attribute, Categorical):
                        cols.append(str(attribute.value))
                        if attr_name not in token_table.categorical_values:
                            token_table.categorical_values[attr_name] = set()
                        token_table.categorical_values[attr_name].add(
                            str(attribute.value)
                        )

                    # For example, form
                    # We create dicts for text attributes to keep track of their IDs
                    # One idea to optimize memory:
                    # - only using a dict (form -> id) and no verticalized file at all to start with
                    # - once the dict's length passes a certain threshold (e.g. 10k diff entries)
                    #   then start writing entries to self._tables whose name start with the text's first letter
                    # - if a text is not found in the dict, look up the file, and if not found in the file, write to it
                    elif any(isinstance(attribute, klass) for klass in (Text, Meta)):
                        name = f"{tok_name}_{attribute.name}"
                        if name not in self._tables:
                            self._tables[name] = LookupTable(
                                tok_name, attribute.name, path, config
                            )
                        table = self._tables[name]
                        text = str(attribute.value)
                        id = table.get_id(text)
                        cols.append(str(id))

                    # For example, head
                    elif isinstance(attribute, Dependency):
                        # token_have_dependencies = True
                        name = attribute.name
                        if name not in self._tables:
                            self._tables[name] = Table(name, path)
                            self._tables[name].write(
                                [
                                    "head",
                                    "dependent",
                                    "udep",
                                    "left_anchor",
                                    "right_anchor",
                                ]
                            )
                        table = self._tables[name]
                        if str(segment.id) not in table.deps:
                            info = Info(segment=segment, document=doc)
                            table.deps[str(segment.id)] = {info: None}
                        deps = table.deps[str(segment.id)]
                        head_id = attribute.value
                        # We assume a new head necessarily means all of the previous head's dependencies have been parsed
                        if head_id == "" and deps:
                            # head_id is None: this is a new head, process the previous one
                            self.write_token_deps(
                                self._tables[name], working_on=str(segment.id)
                            )
                        deps[token.id] = {
                            "head_id": head_id,
                            "nested_set": NestedSet(
                                token.id, attribute.label, token_table.cursor
                            ),
                        }
                        self._tables[name].cursor += 1

                # If this token doesn't have an attribute for an aligned entity, close any pending one
                for aligned_entity in aligned_entities:
                    if aligned_entity in [a.lower() for a in non_null_attributes]:
                        continue
                    self.close_aligned_entity(aligned_entity, path, aligned_entities)

                left_char_range = self.char_range_cur
                self.char_range_cur += len(token.attributes["form"].value) - (
                    0 if token.spaceAfter else 1
                )
                cols.append(f"[{str(left_char_range)},{str(self.char_range_cur)})")
                self.char_range_cur += 1
                if token.frame_range:
                    has_frame_range = True  # Keep it here too for iterations where non_null_attributes is already set
                    left_frame_range, right_frame_range = token.frame_range
                    left_frame_range += offset_frame_range
                    right_frame_range += offset_frame_range
                    if right_frame_range <= left_frame_range:
                        right_frame_range = left_frame_range + 1
                    cols.append(f"[{str(left_frame_range)},{str(right_frame_range)})")
                    if current_document:
                        current_document.frame_range[1] = (
                            offset_frame_range + token.frame_range[1]
                        )
                    if frame_range_segment_start is None:
                        frame_range_segment_start = (
                            offset_frame_range + token.frame_range[0]
                        )
                    self.frame_range_cur = right_frame_range
                cols.append(str(segment.id))
                token_table.write(cols)
                token_table.cursor += 1

            segment_table = self._tables["segment"]
            # if segment_table.cursor == 1:
            if not segment_table.col_names:
                col_names = [f"{seg_name}_id", "char_range"]
                if has_frame_range:
                    col_names.append("frame_range")
                # Add the names of all segment attributes
                for a in segment.attributes:
                    if a in aligned_entities_segment:
                        continue
                    col_names.append(a)
                segment_table.write(col_names)
                segment_table.col_names = col_names
            cols = [str(segment.id)]
            cols.append(f"[{char_range_segment_start},{self.char_range_cur-1})")
            if has_frame_range:
                frame_range_segment_end = self.frame_range_cur
                if frame_range_segment_end <= frame_range_segment_start:
                    frame_range_segment_end = frame_range_segment_start + 1
                cols.append(
                    f"[{str(frame_range_segment_start)},{str(frame_range_segment_end)})"
                )
            # Add all segment attributes
            for a in segment.attributes.values():
                aname_low = a.name.lower()
                if aname_low in aligned_entities_segment:
                    self.aligned_entity(segment, path, a, aligned_entities_segment)
                else:
                    if a.name not in segment_table.col_names:
                        continue
                    cols.append(a.value)
                    if a.name not in segment_table.categorical_values:
                        segment_table.categorical_values[a.name] = set()
                    segment_table.categorical_values[a.name].add(str(a.value))
            # If this segment doesn't have an attribute for one the aligned entities, close it
            for aligned_entity in aligned_entities_segment:
                if aligned_entity in [a.lower() for a in segment.attributes.keys()]:
                    continue
                self.close_aligned_entity(
                    aligned_entity, path, aligned_entities_segment
                )
            # Now write to the file and update cursor
            segment_table.write(cols)
            segment_table.cursor += 1

            # FTS VECTOR if no dependencies
            # Always run for now, since we don't use the same value for "LABEL_IN" and "LABELS_OUT"
            if not token_have_dependencies:
                name = "fts_vector"
                if name not in self._tables:
                    self._tables[name] = Table(name, path)
                fts_table = self._tables[name]
                vector = []
                for n, token in enumerate(segment.tokens, start=1):
                    attributes_to_fts = []
                    for an in non_null_attributes:
                        a = token.attributes[an]
                        if (
                            any(isinstance(a, k) for k in (Categorical, Text))
                            and an.lower() not in aligned_entities
                        ):
                            attributes_to_fts.append(a)
                        elif isinstance(
                            a, Dependency
                        ):  # same value for LABEL_IN and LABELS_OUT
                            attributes_to_fts.append(a)
                            attributes_to_fts.append(a)
                    for i, a in enumerate(attributes_to_fts, start=1):
                        vl = re.sub(r"'", "''", a.value)
                        vector.append(f"'{i}{vl}':{n}")
                cols[1:] = [" ".join(vector)]
                if fts_table.cursor == 1:
                    fts_table.write([f"{seg_name}_id", "vector"])
                fts_table.write(cols)
                fts_table.cursor += 1

        if token_have_dependencies:
            # Write any pending dependencies
            for _, tab in self._tables.items():
                if not tab.deps:
                    continue
                self.write_token_deps(tab)

        # Add any pending aligned entities
        for ename in aligned_entities:
            self.close_aligned_entity(ename, path, aligned_entities)
        for ename in aligned_entities_segment:
            self.close_aligned_entity(ename, path, aligned_entities_segment)

        if current_document is None:
            # No new document marker found when parsing: create an all-encompassing one
            current_document = Document()
            if default_doc:
                current_document.attributes["meta"] = Meta("meta", default_doc)
            current_document.char_range_start = char_range_start

        # Write the last document
        self.upload_new_doc(
            current_document, self._tables["document"], doc_name=doc_name
        )

        for l, lp in self.config["layer"].items():
            table_key = next(
                (v for k, v in self.config["firstClass"].items() if k == l), l
            ).lower()
            if table_key not in self._tables:
                continue
            for a, ap in lp.get("attributes", {}).items():
                categorical_values = self._tables[table_key].categorical_values.get(a)
                if not categorical_values:
                    continue
                if ap.get("type") == "categorical" and not ap.get("isGlobal"):
                    ap["values"] = ap.get("values", [])
                    for v in categorical_values:
                        if v in ap["values"]:
                            continue
                        ap["values"].append(v)

        # for _, v in self._tables.items():
        #     v['file'].close()

    def generate_upload_files(self, content):
        """
        Return ([sentences], (doc_id,char_range,meta)) for a given document file
        """

        sentences = (sent for sent in content.split("\n\n") if sent)

        proc_sentences = []

        ncols = 0
        for sentence in sentences:
            lines = [x for x in sentence.split("\n") if x]

            sent = Sentence(lines, self)
            if sent._lines:
                sent.process()
                proc_sentences.append(sent)
                ncols = max([ncols, *[len(l) for l in sent.proc_lines]])

        for s in proc_sentences:
            for l in s.proc_lines:
                for _ in range(ncols - len(l)):
                    l.append("")

        if proc_sentences:
            doc = self.compute_doc(
                content,
                proc_sentences[0].proc_lines[0][6],
                proc_sentences[-1].proc_lines[-1][6],
            )
            # self.compute_doc()
            return proc_sentences, doc
        else:
            return None, None
