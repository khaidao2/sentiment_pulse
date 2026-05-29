import os
import jinja2

def render_template(template_name: str, context: dict) -> str:
    """Renders a Jinja2 template with the given context."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(current_dir, "templates")
    
    loader = jinja2.FileSystemLoader(templates_dir)
    env = jinja2.Environment(loader=loader)
    template = env.get_template(template_name)
    
    return template.render(context)

def write_rendered_file(output_path: str, content: str):
    """Writes rendered template content to a file, creating directories if needed."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Generated file: {output_path}")
