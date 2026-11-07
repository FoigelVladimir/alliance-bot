from functools import lru_cache
from hashlib import file_digest
from pathlib import Path

from django.conf import settings

MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
DIMENSIONS = 384
INPUT_TOKENS = 512
FRAGMENT_TOKENS = 400
CHECKSUMS = {
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
}


def split_text(text, token_count):
    fragments = []
    offset = 0
    while offset < len(text):
        end = min(offset + 2048, len(text))
        while token_count(text[offset:end]) > FRAGMENT_TOKENS:
            if end - offset <= 1:
                raise ValueError("A source character exceeds the token budget")
            end = offset + (end - offset) // 2
        if end < len(text):
            boundary = max(text.rfind(" ", offset, end), text.rfind("\n", offset, end)) + 1
            if boundary >= offset + max(1, (end - offset) // 2):
                if token_count(text[offset:boundary]) <= FRAGMENT_TOKENS:
                    end = boundary
        fragments.append(text[offset:end])
        offset = end
    return fragments


class E5:
    def __init__(self, root):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        for name, expected in CHECKSUMS.items():
            with (root / name).open("rb") as artifact:
                if file_digest(artifact, "sha256").hexdigest() != expected:
                    raise ValueError(f"E5 checksum mismatch: {name}")
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.pad_token = self.tokenizer.token_to_id("<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(root / "onnx/model.onnx"), sess_options=options, providers=["CPUExecutionProvider"],
        )

    def token_count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def passages(self, fragments):
        return self.encode(["passage: " + fragment for fragment in fragments])

    def encode(self, inputs):
        import numpy as np

        encodings = self.tokenizer.encode_batch(inputs)
        width = max(len(item.ids) for item in encodings)
        if width > INPUT_TOKENS:
            raise ValueError("E5 input exceeds its token budget")
        shape = (len(inputs), width)
        ids = np.full(shape, self.pad_token, dtype=np.int64)
        mask = np.zeros(shape, dtype=np.int64)
        types = np.zeros(shape, dtype=np.int64)
        for index, encoding in enumerate(encodings):
            length = len(encoding.ids)
            ids[index, :length] = encoding.ids
            mask[index, :length] = encoding.attention_mask
            types[index, :length] = encoding.type_ids
        available = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
        outputs = self.session.run(["last_hidden_state"], {
            item.name: available[item.name] for item in self.session.get_inputs()
        })
        pooled = (outputs[0] * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        if pooled.shape != (len(inputs), DIMENSIONS) or not np.isfinite(pooled).all() or np.any(norms <= 0):
            raise ValueError("Invalid E5 output")
        return (pooled / norms).tolist()


@lru_cache(maxsize=1)
def get_embedder():
    if not settings.KNOWLEDGE_E5_ROOT:
        raise ValueError("Set KNOWLEDGE_E5_ROOT to the verified local model directory")
    return E5(Path(settings.KNOWLEDGE_E5_ROOT))
