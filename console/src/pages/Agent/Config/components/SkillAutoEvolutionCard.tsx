import { Form, Card, Switch, InputNumber } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import styles from "../index.module.less";

export function SkillAutoEvolutionCard() {
  const { t } = useTranslation();

  return (
    <Card
      className={styles.formCard}
      title={t("agentConfig.skillAutoEvolutionTitle")}
    >
      <Form.Item
        label={t("agentConfig.skillAutoEvolutionEnabled")}
        name="skill_auto_evolution_enabled"
        valuePropName="checked"
        tooltip={t("agentConfig.skillAutoEvolutionEnabledTooltip")}
      >
        <Switch />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.skillAutoEvolutionMaxIters")}
        name="skill_auto_evolution_max_iters"
        rules={[
          {
            required: true,
            message: t("agentConfig.skillAutoEvolutionMaxItersRequired"),
          },
          {
            type: "number",
            min: 1,
            max: 64,
            message: t("agentConfig.skillAutoEvolutionMaxItersRange"),
          },
        ]}
        tooltip={t("agentConfig.skillAutoEvolutionMaxItersTooltip")}
      >
        <InputNumber style={{ width: "100%" }} min={1} max={64} step={1} />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.skillAutoEvolutionMaxHistoryMessages")}
        name="skill_auto_evolution_max_history_messages"
        rules={[
          {
            required: true,
            message: t(
              "agentConfig.skillAutoEvolutionMaxHistoryMessagesRequired",
            ),
          },
          {
            type: "number",
            min: 1,
            max: 500,
            message: t("agentConfig.skillAutoEvolutionMaxHistoryMessagesRange"),
          },
        ]}
        tooltip={t("agentConfig.skillAutoEvolutionMaxHistoryMessagesTooltip")}
      >
        <InputNumber style={{ width: "100%" }} min={1} max={500} step={1} />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.skillAutoEvolutionMinToolCalls")}
        name="skill_auto_evolution_min_tool_calls"
        rules={[
          {
            required: true,
            message: t("agentConfig.skillAutoEvolutionMinToolCallsRequired"),
          },
          {
            type: "number",
            min: 0,
            max: 500,
            message: t("agentConfig.skillAutoEvolutionMinToolCallsRange"),
          },
        ]}
        tooltip={t("agentConfig.skillAutoEvolutionMinToolCallsTooltip")}
      >
        <InputNumber style={{ width: "100%" }} min={0} max={500} step={1} />
      </Form.Item>

      <Form.Item
        label={t("agentConfig.skillAutoEvolutionReload")}
        name="skill_auto_evolution_reload"
        valuePropName="checked"
        tooltip={t("agentConfig.skillAutoEvolutionReloadTooltip")}
      >
        <Switch />
      </Form.Item>
    </Card>
  );
}
