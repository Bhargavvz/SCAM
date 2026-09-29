from agent.bank_setup import DIRECTIVES, DISPOSITION, MISSION, TRAITS, configure_bank, reflect_mission


class FakeBankClient:
    def __init__(self, with_list=True):
        self.banks, self.config, self.directives = set(), {}, []
        if not with_list:
            self.list_directives = None  # simulate a client without the method

    def create_bank(self, bank_id):
        if bank_id in self.banks:
            raise RuntimeError("409 bank exists")
        self.banks.add(bank_id)

    def update_bank_config(self, bank_id, **kw):
        self.config[bank_id] = kw

    def create_directive(self, bank_id, name, content):
        self.directives.append((bank_id, name, content))

    def list_directives(self, bank_id):
        return [{"name": n} for b, n, _ in self.directives if b == bank_id]


def test_configure_is_idempotent(tmp_path):
    c = FakeBankClient()
    configure_bank(c, "supply-chain-memory", tmp_path / "state.json")
    configure_bank(c, "supply-chain-memory", tmp_path / "state.json")
    assert len(c.directives) == 8
    cfg = c.config["supply-chain-memory"]
    assert cfg["reflect_mission"] == reflect_mission()
    assert {k: cfg[k] for k in DISPOSITION} == {"disposition_skepticism": 4, "disposition_literalism": 4,
                                                 "disposition_empathy": 2}


def test_state_file_used_when_client_cannot_list(tmp_path):
    c = FakeBankClient(with_list=False)
    configure_bank(c, "b", tmp_path / "state.json")
    configure_bank(c, "b", tmp_path / "state.json")
    assert len(c.directives) == 8


def test_prompt_text_is_verbatim():
    assert MISSION.startswith("Institutional supply-chain memory for a multi-plant consumer-goods manufacturer.")
    assert MISSION.endswith("recommend responses grounded in what worked before.")
    assert len(DIRECTIVES) == 8 and len(TRAITS) == 4
    assert DIRECTIVES[2][1] == ("Never recommend cancelling a purchase order without first checking open commitments "
                                "with that supplier.")
    assert all(t in reflect_mission() for t in TRAITS)
